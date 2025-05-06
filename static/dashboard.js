// Dashboard JavaScript
document.addEventListener('DOMContentLoaded', function() {
    // Connect to Socket.IO server
    const socket = io();
    
    // State tracking
    let isSystemRunning = false;
    
    // Elements
    const videoStream = document.getElementById('videoStream');
    const detectionOverlay = document.getElementById('detectionOverlay');
    const fireCount = document.getElementById('fireCount');
    const smokeCount = document.getElementById('smokeCount');
    const logEntries = document.getElementById('logEntries');
    const alertList = document.getElementById('alertList');
    const startBtn = document.getElementById('startBtn');
    const stopBtn = document.getElementById('stopBtn');
    const resetBtn = document.getElementById('resetBtn');
    const modelName = document.getElementById('modelName');
    const confidenceThreshold = document.getElementById('confidenceThreshold');
    const iouThreshold = document.getElementById('iouThreshold');
    const videoSource = document.getElementById('videoSource');
    const alertCooldown = document.getElementById('alertCooldown');
    
    // Stats synchronization function
    function syncStats() {
        fetch('/api/detection_counts')
            .then(response => response.json())
            .then(data => {
                // Always update the UI with the latest data from server
                fireCount.textContent = data.Fire || 0;
                smokeCount.textContent = data.Smoke || 0;
                
                // Update chart
                detectionChart.data.datasets[0].data = [
                    data.Fire || 0,
                    data.Smoke || 0
                ];
                detectionChart.update();
            })
            .catch(error => console.error('Error syncing stats:', error));
    }
    
    // Chart setup
    const ctx = document.getElementById('detectionChart').getContext('2d');
    const detectionChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['Fire', 'Smoke'],
            datasets: [{
                label: 'Detections',
                data: [0, 0],
                backgroundColor: [
                    'rgba(255, 99, 132, 0.7)',
                    'rgba(128, 128, 128, 0.7)'
                ],
                borderColor: [
                    'rgba(255, 99, 132, 1)',
                    'rgba(128, 128, 128, 1)'
                ],
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: {
                        precision: 0
                    }
                }
            }
        }
    });
    
    // Function to update stats display
    function updateStats(data) {
        fireCount.textContent = data.Fire || 0;
        smokeCount.textContent = data.Smoke || 0;
        updateChart();
    }
    
    function updateChart() {
        detectionChart.data.datasets[0].data = [
            parseInt(fireCount.textContent) || 0,
            parseInt(smokeCount.textContent) || 0
        ];
        detectionChart.update();
    }
    
    // Function to update system status
    function updateSystemStatus(isActive) {
        const statusIndicator = document.getElementById('statusIndicator');
        const statusText = document.getElementById('statusText');
        
        if (isActive) {
            statusIndicator.className = 'status-indicator online';
            statusText.textContent = 'System Active';
        } else {
            statusIndicator.className = 'status-indicator offline';
            statusText.textContent = 'System Offline';
        }
    }
    
    // Fetch initial data
    fetchStats();
    fetchLogs();
    
    // Set up video stream
    videoStream.src = "/video_feed";
    videoStream.onerror = function() {
        detectionOverlay.textContent = "Video stream unavailable";
        detectionOverlay.style.backgroundColor = "rgba(255, 0, 0, 0.7)";
    };
    
    // Socket.IO event listeners
    socket.on('connect', function() {
        console.log('Connected to server');
    });
    
    socket.on('detection_update', function(data) {
        if (data.status) {
            detectionOverlay.textContent = `${data.status} Detected!`;
            detectionOverlay.className = 'detection-overlay ' + data.status.toLowerCase();
        } else {
            detectionOverlay.textContent = 'No Detection';
            detectionOverlay.className = 'detection-overlay';
        }
    });
    
    socket.on('stats_update', function(data) {
        updateStats(data);
    });
    
    socket.on('log_update', function(data) {
        addLogEntry(data);
    });
    
    socket.on('alert_sent', function(data) {
        addAlertEntry(data);
    });
    
    socket.on('system_status', function(data) {
        isSystemRunning = data.active;
        updateControlButtons(data.active);
    });
    
    // Button event listeners
    startBtn.addEventListener('click', function() {
        fetch('/api/control', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                action: 'start'
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'started') {
                isSystemRunning = true;
                updateControlButtons(true);
                
                // Force reload the video stream
                videoStream.src = "/video_feed" + "?t=" + new Date().getTime();
            }
        });
    });
    
    stopBtn.addEventListener('click', function() {
        // First update UI to prevent multiple clicks
        updateControlButtons(false);
        
        // Display stopping message
        detectionOverlay.textContent = 'Stopping Detection...';
        detectionOverlay.style.backgroundColor = "rgba(255, 165, 0, 0.7)";
        
        fetch('/api/control', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                action: 'stop'
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'stopped') {
                isSystemRunning = false;
                
                // Update the detection overlay
                detectionOverlay.textContent = 'Detection Stopped';
                detectionOverlay.className = 'detection-overlay';
                
                // Force page reload after short delay
                setTimeout(() => {
                    window.location.reload();
                }, 2000);
            } else {
                alert("Failed to stop detection. Reloading page...");
                window.location.reload();
            }
        })
        .catch(error => {
            console.error("Error stopping detection:", error);
            alert("Error stopping detection. Reloading page...");
            window.location.reload();
        });
    });
    
    // Add reset button event listener if it exists
    if (resetBtn) {
        resetBtn.addEventListener('click', function() {
            if (confirm('Are you sure you want to reset the system? This will clear all state.')) {
                window.location.href = '/reset';
            }
        });
    }
    
    // Cleanup on page unload
    window.addEventListener('beforeunload', function() {
        // Try to stop the system when leaving the page
        if (stopBtn.disabled === false) {
            fetch('/api/control', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    action: 'stop'
                }),
                // Use keepalive to ensure the request completes even if the page is unloading
                keepalive: true
            });
        }
    });
    
    // Functions
    function fetchStats() {
        fetch('/api/stats')
            .then(response => response.json())
            .then(data => {
                // Update detection counts
                updateStats(data.detections);
                
                // Update model info
                modelName.textContent = data.model.name;
                confidenceThreshold.textContent = data.model.confidence_threshold.toFixed(2);
                iouThreshold.textContent = data.model.iou_threshold.toFixed(2);
                videoSource.textContent = data.system.video_source;
                alertCooldown.textContent = `${data.system.alert_cooldown} seconds`;
                
                // Update system status
                isSystemRunning = data.system.active;
                updateControlButtons(data.system.active);
            });
    }
    
    function fetchLogs() {
        fetch('/api/logs')
            .then(response => response.json())
            .then(data => {
                logEntries.innerHTML = '';
                data.forEach(entry => {
                    addLogEntry(entry, false);
                });
            });
    }
    
    function addLogEntry(entry, scrollToBottom = true) {
        const logEntry = document.createElement('div');
        logEntry.className = `log-entry ${entry.level.toLowerCase()}`;
        logEntry.textContent = `${entry.timestamp} - ${entry.level}: ${entry.message}`;
        logEntries.appendChild(logEntry);
        
        if (scrollToBottom) {
            logEntries.scrollTop = logEntries.scrollHeight;
        }
    }
    
    function addAlertEntry(data) {
        const alertItem = document.createElement('li');
        alertItem.className = `alert-item ${data.type.toLowerCase()}`;
        
        const alertTime = document.createElement('span');
        alertTime.className = 'alert-time';
        alertTime.textContent = data.time;
        
        const alertType = document.createElement('span');
        alertType.className = 'alert-type';
        alertType.textContent = `${data.type} Detected`;
        
        alertItem.appendChild(alertType);
        alertItem.appendChild(alertTime);
        alertList.prepend(alertItem);
        
        // Limit list to 10 items
        if (alertList.children.length > 10) {
            alertList.removeChild(alertList.lastChild);
        }
    }
    
    function updateControlButtons(isActive) {
        if (isActive) {
            startBtn.disabled = true;
            stopBtn.disabled = false;
        } else {
            startBtn.disabled = false;
            stopBtn.disabled = true;
        }
        updateSystemStatus(isActive);
    }
    
    // Call syncStats on load and set up polling intervals
    syncStats();
    setInterval(syncStats, 3000);  // Sync stats every 3 seconds for reliability
    setInterval(fetchStats, 10000); // Refresh all stats every 10 seconds
});
