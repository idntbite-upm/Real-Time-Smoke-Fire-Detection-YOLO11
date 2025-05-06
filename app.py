import os
import cv2
import logging
import threading
import time
import json
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, Response, jsonify, request, redirect, url_for
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global variables
frame_buffer = None
detection_status = None
latest_logs = []
detection_count = {"Fire": 0, "Smoke": 0}
stats_lock = threading.Lock()  # Lock for thread-safe stats updates
system_active = False
processing_thread = None
alert_cooldown = Config.ALERT_COOLDOWN
last_alert_time = 0  # Initialize the last alert time

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
    global frame_buffer, detection_status, last_alert_time
    
    # Use OpenCV to capture video
    if str(Config.VIDEO_SOURCE).isdigit():
        cap = cv2.VideoCapture(int(Config.VIDEO_SOURCE))  # For webcam
    else:
        cap = cv2.VideoCapture(str(Config.VIDEO_SOURCE))  # For video file
    
    if not cap.isOpened():
        logger.error(f"Failed to open video source: {Config.VIDEO_SOURCE}")
        return
    
    logger.info(f"Started video processing from: {Config.VIDEO_SOURCE}")
    
    frame_count = 0
    while system_active:
        # Check if we should exit early
        if not system_active:
            break
            
        # Check system_active more frequently
        frame_count += 1
        if frame_count % 10 == 0 and not system_active:
            break
            
        success, frame = cap.read()
        if not success:
            # If video file ends, loop back to beginning
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
        # Process frame for detection
        processed_frame, detection = detector.process_frame(frame)
        frame_buffer = processed_frame.copy()
        
        # Update detection status when it changes
        if detection != detection_status:
            old_status = detection_status
            detection_status = detection
            
            if detection:
                # Use a lock when updating the detection count
                with stats_lock:
                    # Explicitly increment detection count
                    if detection == "Fire":
                        detection_count["Fire"] = detection_count.get("Fire", 0) + 1
                    elif detection == "Smoke":
                        detection_count["Smoke"] = detection_count.get("Smoke", 0) + 1
                
                # Emit the updated counts
                socketio.emit('detection_update', {'status': detection})
                socketio.emit('stats_update', dict(detection_count))
                
                # Alert logic with cooldown
                current_time = time.time()
                if (current_time - last_alert_time) > alert_cooldown:
                    logger.warning(f"🔥 {detection} Detected! Sending alert")
                    notification_service.send_alert(processed_frame, detection)
                    socketio.emit('alert_sent', {'type': detection, 'time': datetime.now().strftime('%H:%M:%S')})
                    last_alert_time = current_time

        # Force emit stats periodically (every 30 frames)
        if frame_count % 30 == 0:
            socketio.emit('stats_update', dict(detection_count))
        
        # Convert to JPEG format
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
    frame_gen = generate_frames()
    try:
        # Use a local flag that's checked more frequently
        local_active = True
        while local_active and system_active:
            try:
                next(frame_gen)
                # Check if we should stop - check both global and local flag
                local_active = system_active
                # Add a small sleep to prevent CPU hogging
                time.sleep(0.01)
            except StopIteration:
                break
    except Exception as e:
        logger.error(f"Error in video processing: {str(e)}")
    finally:
        logger.info("Video processing thread completed")

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
    with stats_lock:
        current_counts = dict(detection_count)
    return jsonify({
        'detections': current_counts,
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

@app.route('/api/detection_counts', methods=['GET'])
def api_detection_counts():
    """Return current detection counts"""
    global detection_count
    with stats_lock:
        current_counts = dict(detection_count)
    return jsonify(current_counts)

@app.route('/api/control', methods=['POST'])
def api_control():
    """Control system operation"""
    global system_active, processing_thread
    
    action = request.json.get('action')
    logger.info(f"Control action received: {action}")
    
    if action == 'start' and not system_active:
        system_active = True
        processing_thread = threading.Thread(target=process_video)
        processing_thread.daemon = True
        processing_thread.start()
        logger.info("System started")
        
        # Broadcasting system active state to all clients
        socketio.emit('system_status', {'active': True})
        
        return jsonify({'status': 'started'})
        
    elif action == 'stop' and system_active:
        # First set the flag to stop the thread
        system_active = False
        
        # Wait for thread to terminate (with timeout)
        if processing_thread and processing_thread.is_alive():
            try:
                processing_thread.join(timeout=3.0)
            except Exception as e:
                logger.error(f"Error joining thread: {str(e)}")
        
        # Broadcasting system inactive state to all clients
        socketio.emit('system_status', {'active': False})
        
        logger.info("System stopped")
        return jsonify({'status': 'stopped'})
        
    return jsonify({'status': 'unchanged'})

@app.route('/reset')
def reset_system():
    """Emergency reset of the system"""
    global system_active, processing_thread, detection_count
    
    logger.warning("Emergency reset requested")
    
    # Reset all state
    system_active = False
    with stats_lock:
        detection_count = {"Fire": 0, "Smoke": 0}
    
    # Try to terminate thread
    if processing_thread and processing_thread.is_alive():
        try:
            processing_thread.join(timeout=1.0)
        except Exception as e:
            logger.error(f"Error joining thread during reset: {str(e)}")
    
    socketio.emit('system_status', {'active': False})
    socketio.emit('stats_update', detection_count)
    
    return redirect(url_for('index'))

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info("Socket client connected")
    with stats_lock:
        current_counts = dict(detection_count)
    emit('stats_update', current_counts)
    emit('system_status', {'active': system_active})

if __name__ == '__main__':
    logger.info("Starting application")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
