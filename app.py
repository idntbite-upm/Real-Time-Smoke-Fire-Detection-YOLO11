import os
import cv2
import logging
import threading
import time
import json
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit

# Import existing components
from src.config import Config, setup_logging
from src.fire_detector import Detector
from src.notification_service import NotificationService

# Initialize Flask app
app = Flask(__name__, 
            static_folder='static',
            template_folder='templates')
app.config['SECRET_KEY'] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables
frame_buffer = None
detection_status = None
latest_logs = []
detection_count = {"Fire": 0, "Smoke": 0}
system_active = False
processing_thread = None
alert_cooldown = Config.ALERT_COOLDOWN
last_alert_time = 0

# Initialize system components
setup_logging()
logger = logging.getLogger(__name__)
detector = Detector(Config.MODEL_PATH)
notification_service = NotificationService(Config)

# Configure logging handler to capture logs
class LogHandler(logging.Handler):
    def __init__(self, buffer_size=100):
        super().__init__()
        self.buffer = []
        self.buffer_size = buffer_size
        
    def emit(self, record):
        log_entry = {
            'timestamp': datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': self.format(record)
        }
        self.buffer.append(log_entry)
        if len(self.buffer) > self.buffer_size:
            self.buffer.pop(0)
        
        # Emit to websocket
        socketio.emit('log_update', log_entry)

log_handler = LogHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
logging.getLogger().addHandler(log_handler)

def get_logs():
    return log_handler.buffer

def generate_frames():
    """Generate frames from video source with detection overlay"""
    global frame_buffer, detection_status
    
    # Use OpenCV to capture video
    if str(Config.VIDEO_SOURCE).isdigit():
        cap = cv2.VideoCapture(int(Config.VIDEO_SOURCE))  # For webcam
    else:
        cap = cv2.VideoCapture(str(Config.VIDEO_SOURCE))  # For video file
    
    if not cap.isOpened():
        logger.error(f"Failed to open video source: {Config.VIDEO_SOURCE}")
        return
    
    logger.info(f"Started video processing from: {Config.VIDEO_SOURCE}")
    
    while system_active:
        success, frame = cap.read()
        if not success:
            # If video file ends, loop back to beginning
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
        # Process frame for detection
        processed_frame, detection = detector.process_frame(frame)
        frame_buffer = processed_frame.copy()
        
        if detection != detection_status:
            detection_status = detection
            if detection:
                logger.info(f"Detection status changed: {detection}")
                socketio.emit('detection_update', {'status': detection})
                
                # Handle detection counting
                detection_count[detection] = detection_count.get(detection, 0) + 1
                socketio.emit('stats_update', detection_count)
                
                # Alert logic with cooldown
                current_time = time.time()
                if (current_time - last_alert_time) > alert_cooldown:
                    logger.warning(f"🔥 {detection} Detected! Sending alert")
                    notification_service.send_alert(processed_frame, detection)
                    socketio.emit('alert_sent', {'type': detection, 'time': datetime.now().strftime('%H:%M:%S')})
                    last_alert_time = current_time
        
        # Convert the frame to JPEG format
        ret, buffer = cv2.imencode('.jpg', processed_frame)
        if not ret:
            continue
            
        # Yield the frame in bytes
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    
    # Clean up
    cap.release()
    logger.info("Video processing stopped")

def process_video():
    """Background thread for video processing"""
    for _ in generate_frames():
        pass

@app.route('/')
def index():
    """Render dashboard home page"""
    return render_template('dashboard.html')

@app.route('/video_feed')
def video_feed():
    """Video streaming route"""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/logs')
def api_logs():
    """Return recent logs as JSON"""
    return jsonify(get_logs())

@app.route('/api/stats')
def api_stats():
    """Return detection statistics"""
    global detection_count
    return jsonify({
        'detections': detection_count,
        'model': {
            'name': Config.MODEL_PATH.name,
            'confidence_threshold': detector.min_confidence,
            'iou_threshold': detector.iou_threshold
        },
        'system': {
            'active': system_active,
            'alert_cooldown': alert_cooldown,
            'video_source': str(Config.VIDEO_SOURCE)
        }
    })

@app.route('/api/control', methods=['POST'])
def api_control():
    """Control system operation"""
    global system_active, processing_thread
    
    action = request.json.get('action')
    
    if action == 'start' and not system_active:
        system_active = True
        processing_thread = threading.Thread(target=process_video)
        processing_thread.daemon = True
        processing_thread.start()
        logger.info("System started")
        return jsonify({'status': 'started'})
        
    elif action == 'stop' and system_active:
        system_active = False
        if processing_thread:
            processing_thread.join(timeout=1.0)
        logger.info("System stopped")
        return jsonify({'status': 'stopped'})
        
    return jsonify({'status': 'unchanged'})

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    emit('stats_update', detection_count)
    emit('system_status', {'active': system_active})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)