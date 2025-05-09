#!/usr/bin/env python3
"""
Enhanced WhatsApp Notification Test Script with GCS
--------------------------------------------------
Tests the WhatsApp notification system with GCS integration.
"""

import os
import sys
import cv2
import numpy as np
import logging
import tempfile
import requests
from pathlib import Path
from dotenv import load_dotenv
from google.cloud import storage
from google.oauth2 import service_account
import uuid

# Add project root to path to allow importing modules
project_root = Path(__file__).parent.parent if __name__ == "__main__" else Path(__file__).parent
if project_root not in sys.path:
    sys.path.insert(0, str(project_root))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("whatsapp_test")

def create_test_image(output_path=None):
    """Create a test image for notification testing"""
    # Create a simple test image with a fire-like appearance
    img = np.zeros((400, 600, 3), dtype=np.uint8)
    
    # Draw a background
    img[:, :] = (30, 30, 60)  # Dark blue background
    
    # Create a fire-like effect
    for y in range(100, 350):
        radius = int(150 - abs((y - 225) / 2))
        x_center = 300
        for x in range(x_center - radius, x_center + radius):
            if 0 <= x < 600 and 0 <= y < 400:
                # Distance from center
                dist = np.sqrt((x - x_center)**2 + (y - 225)**2)
                intensity = 1.0 - min(1.0, dist / radius)
                
                # Fire colors (red to yellow gradient)
                red = min(255, int(255 * intensity))
                green = min(255, int(100 * intensity))
                blue = min(255, int(30 * intensity))
                
                # Apply color with some randomness
                if np.random.random() > 0.3:
                    img[y, x] = (blue, green, red)
    
    # Add a warning text
    cv2.putText(
        img, 
        "FIRE DETECTED!", 
        (150, 50), 
        cv2.FONT_HERSHEY_SIMPLEX, 
        1.3, 
        (255, 255, 255), 
        3
    )
    
    # Add timestamp for uniqueness
    cv2.putText(
        img, 
        f"Test: {str(uuid.uuid4())[:8]}", 
        (400, 380), 
        cv2.FONT_HERSHEY_SIMPLEX, 
        0.7, 
        (255, 255, 255), 
        2
    )
    
    # Save the image
    if output_path is None:
        output_path = Path(tempfile.gettempdir()) / f"fire_test_{uuid.uuid4()}.jpg"
    
    cv2.imwrite(str(output_path), img)
    logger.info(f"Created test image at: {output_path}")
    
    return output_path

def test_gcs_upload():
    """Test direct upload to Google Cloud Storage"""
    # Load environment variables
    env_path = project_root / '.env'
    if env_path.exists():
        load_dotenv(env_path)
    else:
        logger.warning(f"No .env file found at {env_path}")
    
    # Check for GCS credentials
    gcs_key_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    bucket_name = os.getenv('GCS_BUCKET_NAME')
    
    if not all([gcs_key_path, bucket_name]):
        logger.error("Missing GCS credentials. Set GOOGLE_APPLICATION_CREDENTIALS and GCS_BUCKET_NAME in .env")
        return False
    
    try:
        # Create test image
        test_image_path = create_test_image()
        
        # Initialize GCS client
        logger.info("Initializing GCS client...")
        credentials = service_account.Credentials.from_service_account_file(gcs_key_path)
        storage_client = storage.Client(credentials=credentials)
        bucket = storage_client.bucket(bucket_name)
        
        # Upload to GCS
        logger.info(f"Uploading test image to GCS bucket {bucket_name}...")
        blob_name = f"tests/gcs_test_{uuid.uuid4()}.jpg"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(test_image_path))
        
        # Make the blob publicly accessible
        blob.make_public()
        public_url = blob.public_url
        
        logger.info(f"✅ GCS Upload successful: {public_url}")
        return True
    
    except Exception as e:
        logger.error(f"❌ GCS upload failed: {str(e)}")
        return False
    finally:
        # Clean up
        if 'test_image_path' in locals() and test_image_path.exists():
            try:
                test_image_path.unlink()
                logger.info("Test image removed")
            except:
                pass

def test_notification_service():
    """Test the notification service with GCS integration"""
    from src.config import Config, setup_logging
    from src.notification_service import NotificationService
    
    try:
        # Setup
        setup_logging()
        notification_service = NotificationService(Config)
        
        if not notification_service.whatsapp_enabled:
            logger.error("WhatsApp notifications not enabled. Check your credentials.")
            return False
        
        # Create test image
        test_image_path = create_test_image()
        
        # Convert image to cv2 format
        image = cv2.imread(str(test_image_path))
        if image is None:
            logger.error(f"Failed to read image: {test_image_path}")
            return False
        
        # Send alert
        logger.info("Sending test alert via notification service...")
        result = notification_service.send_alert(image, "TEST")
        
        # Cleanup
        notification_service.cleanup()
        
        if result:
            logger.info("Alert sent successfully through notification service")
        else:
            logger.error("Failed to send alert through notification service")
        
        return result
    
    except Exception as e:
        logger.error(f"Error in notification service test: {str(e)}")
        return False
    finally:
        # Clean up
        if 'test_image_path' in locals() and test_image_path.exists():
            try:
                test_image_path.unlink()
            except:
                pass

def run_all_tests():
    """Run all available notification tests"""
    results = {}
    
    # Load .env
    env_path = project_root / '.env'
    if env_path.exists():
        load_dotenv(env_path)
        logger.info(f"Loaded environment from {env_path}")
    
    # Test GCS upload
    logger.info("Testing direct GCS upload...")
    results["gcs_upload"] = test_gcs_upload()
    
    # Test notification service
    logger.info("Testing notification service with GCS...")
    results["notification_service"] = test_notification_service()
    
    # Report results
    logger.info("\n--- WhatsApp Notification with GCS Test Results ---")
    for test_name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        logger.info(f"{test_name}: {status}")
    
    # Return overall success
    return any(results.values())

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
