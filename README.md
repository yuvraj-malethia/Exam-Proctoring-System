# Exam Proctoring System

A computer-vision based exam monitoring system that analyzes student head movement and behavioral patterns during examinations.

The system uses facial landmarks, individual calibration, temporal analysis, multi-signal suspicion scoring, identity tracking, behavioral pattern detection, and cross-student correlation to provide invigilators with real-time information about events that may require attention.

> **Status:** Active Development  
> **Type:** Capstone Project  
> **Language:** Python

---

## Overview

Monitoring an examination through video produces a large amount of information, but manually reviewing every student's behavior throughout an entire session is difficult.

This project explores an automated, human-in-the-loop approach to exam monitoring.

Instead of treating a single head movement as evidence of misconduct, the system builds an individual baseline for each student and analyzes behavior over time.

The processing pipeline is approximately:

```text
Camera / Video
      │
      ▼
Face Detection
      │
      ▼
Student Tracking
      │
      ▼
Head Pose Estimation
      │
      ▼
Individual Calibration
      │
      ▼
Signal Processing
 ┌────┼───────────────┐
 │    │               │
 ▼    ▼               ▼
Yaw  Pitch       Kinematics
 │    │               │
 └────┼───────────────┘
      ▼
Temporal Analysis
      │
      ▼
Suspicion Fusion
      │
 ┌────┴─────────────┐
 ▼                  ▼
Behavior Patterns  Cross-Student
                   Correlation
      │                  │
      └────────┬─────────┘
               ▼
        Human Review
               │
        ┌──────┴──────┐
        ▼             ▼
     Evidence      Dashboard
      Capture       & Replay
```

---

## Features

### Multi-Student Monitoring

- Simultaneous monitoring of multiple students
- Independent processing pipeline for each student
- Persistent student/seat tracking
- Expected-student count
- Detection filtering for small and duplicate faces

### Head Pose Analysis

The system extracts facial landmarks and estimates head orientation, including:

- Yaw
- Pitch
- Head orientation
- Deviation from the student's calibrated baseline

### Individual Calibration

Different students naturally sit and move differently.

The system therefore establishes an individual baseline for each student rather than relying entirely on a single global threshold.

Calibration tracks:

- Running mean
- Standard deviation
- Initial baseline
- Adaptive deviation threshold

The calibration implementation uses online statistical estimation.

### Signal Processing

Raw camera measurements can be noisy, so the system applies several processing techniques:

- Kalman filtering
- Rolling-window analysis
- Velocity calculation
- Acceleration calculation
- Adaptive thresholds
- Exponential moving average smoothing

### Temporal Behavior Analysis

Individual movements are not considered in isolation.

The system analyzes behavior over time using:

- Event frequency
- Event duration
- Away-time ratio
- Recent behavioral windows
- Session-level behavior
- Session memory decay

This allows repeated behavior to be analyzed differently from isolated movements.

### Suspicion Scoring

Multiple behavioral signals are combined into a continuously updated suspicion score.

The current scoring pipeline incorporates:

- Position deviation
- Velocity
- Event frequency
- Event duration
- Session behavior

The score is smoothed and converted into monitoring levels/verdicts.

### Behavioral Pattern Detection

The system provides higher-level interpretations of recent behavior in addition to the numerical score.

Examples include patterns such as:

- Repeated short glances
- Sustained looking-away behavior
- Directional behavior
- Burst behavior
- Escalating behavior
- Restless but undirected movement

These patterns provide additional context for human review.

### Cross-Student Correlation

The system can analyze relationships between students rather than evaluating every student independently.

It looks for temporal relationships such as:

- Synchronized movements
- Repeated directional relationships
- Delayed behavioral responses

These correlations are presented as additional signals and do not directly replace individual scoring.

### Identity Tracking

The system includes several mechanisms for maintaining student identity across a session:

- Seat tracking
- Face-position matching
- Face signatures
- Face re-identification
- Occupant archives
- Identity review

When the system cannot confidently determine whether a returning face belongs to a previous occupant, the decision can be passed to the invigilator.

### Student Roster

An invigilator can register students for a session using:

- Student name
- Roll number
- Seat/slot

The roster can also be used to limit the expected number of students detected in the room.

### Evidence Capture

The system can automatically capture evidence when configured behavioral conditions are reached.

Captured evidence can contain:

- Event images
- Event metadata
- Student/seat information
- Timestamp
- Associated behavioral information

Evidence is generated locally during execution and is **not included in this repository**.

### Live Web Dashboard

A Flask-based dashboard provides a browser interface for monitoring a running session.

The dashboard includes:

- Live camera feed
- Student status
- Suspicion scores
- Signal breakdown
- Head-pose information
- Calibration status
- Event timeline
- Risk timeline
- Behavioral patterns
- Cross-student correlations
- Evidence information
- Identity state
- Student roster
- Session statistics

### Session Replay

Recorded sessions can be replayed through the dashboard.

Replay provides a timeline for inspecting previously processed behavior and events.

### Video Processing

The system can process recorded video instead of a live webcam.

This allows the same processing pipeline to be used for:

- Development
- Testing
- Reproducible experiments
- Validation
- Behavior analysis

### Validation & What-If Analysis

The project contains tools for replaying recorded clips and experimenting with configuration parameters.

This makes it possible to evaluate how changes to thresholds and other configuration values affect system behavior.

---

## Architecture

The project is divided into multiple focused modules.

### Core Processing

| Module | Purpose |
|---|---|
| `module_headpose_multi_web.py` | Main multi-student processing loop |
| `student_pipeline.py` | Per-student processing pipeline |
| `head_pose.py` | Head-pose extraction |
| `calibration.py` | Individual calibration |
| `kalman_filter.py` | Signal filtering |
| `kinematics.py` | Velocity and acceleration analysis |
| `temporal.py` | Temporal event analysis |
| `suspicion.py` | Suspicion-score fusion |
| `verdict.py` | Verdict generation |
| `config.py` | System configuration |

### Identity

| Module | Purpose |
|---|---|
| `identity_tracker.py` | Student/seat tracking |
| `face_signature.py` | Face-signature matching |
| `identity_review.py` | Human identity review |
| `occupant_archive.py` | Previous occupant records |
| `roster.py` | Student roster management |

### Behavioral Intelligence

| Module | Purpose |
|---|---|
| `behavior_patterns.py` | Higher-level behavior patterns |
| `correlation.py` | Cross-student behavior correlation |
| `narrative.py` | Human-readable behavioral information |

### Evidence & Session Management

| Module | Purpose |
|---|---|
| `evidence_capture.py` | Evidence generation |
| `event_logger.py` | Event logging |
| `frame_profiler.py` | Frame/session profiling |
| `watchdog.py` | Runtime monitoring |
| `system_monitor.py` | System monitoring |
| `session_replay.py` | Session replay |
| `session_report.py` | Session processing/report logic |

### Video & Validation

| Module | Purpose |
|---|---|
| `video_run.py` | Video input abstraction |
| `video_batch.py` | Batch video processing |
| `record_clip.py` | Video recording |
| `validation.py` | Validation workflow |
| `whatif.py` | Configuration experiments |

### Dashboard

```text
dashboard/
├── index.html
├── login.html
├── login.js
├── dashboard.js
├── session_replay.js
├── digital_twin.js
├── styles.css
├── favicon.svg
└── vendor/
    └── three.min.js
```

The dashboard backend is provided by:

```text
dashboard_server.py
dashboard_auth.py
web_state.py
```

---

## Technology Stack

### Computer Vision

- Python
- OpenCV
- MediaPipe
- NumPy

### Backend

- Flask
- Werkzeug

### Frontend

- HTML
- CSS
- JavaScript
- Server-Sent Events
- MJPEG streaming
- Three.js

### Algorithms & Techniques

- Facial landmark analysis
- Head-pose estimation
- Kalman filtering
- Welford online statistics
- Adaptive thresholds
- Temporal pattern analysis
- Kinematic analysis
- Exponential moving averages
- Multi-signal score fusion
- Face-signature matching
- Cross-student behavioral correlation

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/yuvraj-malethia/Exam-Proctoring-System.git
cd Exam-Proctoring-System
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

Activate it on Windows:

```bash
venv\Scripts\activate
```

On Linux/macOS:

```bash
source venv/bin/activate
```

### 3. Install dependencies

Install the required Python packages:

```bash
pip install opencv-python mediapipe numpy flask werkzeug reportlab
```

> Dependency versions may need to be adjusted depending on your Python version and operating system.

---

## Running the System

### Live Webcam

Start the main monitoring system:

```bash
python module_headpose_multi_web.py
```

The application will attempt to open the default webcam and initialize the multi-student monitoring pipeline.

---

## Web Dashboard

The dashboard can also be started independently in mock/demo mode:

```bash
python dashboard_server.py
```

This starts the dashboard without requiring a live camera and uses simulated data for interface development and testing.

By default, the dashboard is available at:

```text
http://127.0.0.1:5000
```

The actual host and port are controlled through the project configuration.

---

## Dashboard Authentication

The dashboard includes an authentication system for invigilator access.

Create an account:

```bash
python dashboard_auth.py add USERNAME
```

Change a password:

```bash
python dashboard_auth.py passwd USERNAME
```

Remove an account:

```bash
python dashboard_auth.py remove USERNAME
```

List accounts:

```bash
python dashboard_auth.py list
```

Passwords are stored as salted password hashes rather than plaintext.

---

## Video Processing

The main processing engine supports recorded video in addition to live webcam input.

This allows recorded sessions to pass through the same processing pipeline as live sessions.

This is useful for:

- Testing
- Debugging
- Reproducible experiments
- Validation
- Parameter analysis

---

## Detection Pipeline

### 1. Face Detection

The system detects faces within each frame and maintains multiple student tracks.

### 2. Head Pose Estimation

Facial landmarks are used to estimate head orientation.

```text
Yaw
 │
 ├── Horizontal head movement
 │
Pitch
 │
 └── Vertical / downward movement
```

### 3. Calibration

Each student is calibrated independently.

```text
Student
   │
   ▼
Calibration
   │
   ├── Baseline
   └── Standard Deviation
          │
          ▼
   Adaptive Threshold
```

### 4. Signal Processing

Raw measurements are filtered and analyzed for:

- Position
- Velocity
- Acceleration
- Direction
- Deviation

### 5. Temporal Analysis

Detected deviations are accumulated into behavioral events.

The system evaluates:

```text
Frequency
Duration
Away Ratio
Recent Activity
Session Activity
```

### 6. Suspicion Fusion

The behavioral signals are combined into a continuously updated score.

### 7. Behavioral Interpretation

Higher-level pattern detection analyzes the temporal structure of behavior and provides additional context.

### 8. Human Review

The dashboard exposes the underlying signals, score, events, evidence, identity state, and behavioral patterns so that an invigilator can review the situation.

---

## Design Principles

### Individual Baselines

Students naturally have different sitting positions and movement patterns.

The system therefore uses per-student calibration rather than relying entirely on a universal threshold.

### Temporal Context

A single frame or isolated movement should not dominate the analysis.

Behavior is evaluated over time.

### Explainability

The system exposes the underlying behavioral signals and detected patterns instead of presenting only a final numerical score.

### Human-in-the-Loop

Computer vision cannot reliably determine the meaning of every student movement.

The system therefore provides information and evidence for an invigilator to review.

### Separation of Processing and UI

The computer-vision engine and dashboard are separated.

The browser receives serialized state from the processing engine rather than implementing the detection algorithms itself.

---

## Limitations

This project is an experimental capstone system and should not be treated as an autonomous cheating-detection system.

Important limitations include:

- Head movement is not proof of cheating.
- Students may naturally look away or move frequently.
- Poor lighting can affect detection.
- Camera positioning affects the quality of tracking.
- Faces can be temporarily occluded.
- Face identification can be uncertain.
- Looking down can have legitimate explanations.
- Cross-student correlations are signals for review, not proof of collaboration.
- Behavioral thresholds require validation on representative examination footage.
- The system is dependent on the camera's field of view.

The system is designed as an **assistive monitoring tool with human oversight**.

---

## Project Structure

```text
Exam-Proctoring-System/
│
├── module_headpose_multi_web.py
│
├── config.py
├── head_pose.py
├── calibration.py
├── kalman_filter.py
├── kinematics.py
├── temporal.py
├── suspicion.py
├── verdict.py
├── student_pipeline.py
│
├── identity_tracker.py
├── face_signature.py
├── identity_review.py
├── occupant_archive.py
├── roster.py
│
├── behavior_patterns.py
├── correlation.py
├── narrative.py
│
├── evidence_capture.py
├── event_logger.py
├── frame_profiler.py
├── watchdog.py
├── system_monitor.py
│
├── video_run.py
├── video_batch.py
├── record_clip.py
├── session_replay.py
├── session_report.py
├── validation.py
├── whatif.py
│
├── dashboard_auth.py
├── dashboard_server.py
├── web_state.py
│
├── camera_overlays.py
├── cv_dashboard.py
├── dev_panel.py
├── developer_ui_state.py
├── mock_engine.py
├── clock.py
│
└── dashboard/
    ├── index.html
    ├── login.html
    ├── login.js
    ├── dashboard.js
    ├── session_replay.js
    ├── digital_twin.js
    ├── styles.css
    └── vendor/
        └── three.min.js
```

Runtime-generated directories such as `evidence/` are intentionally not included in the repository.

---

## Project Status

This project is currently under active development as a capstone project.

The architecture and detection pipeline are still being refined and evaluated.

---

## Disclaimer

This project is intended for educational and research purposes.

Automated behavioral signals should not be treated as definitive evidence of academic misconduct. Any action affecting a student should involve appropriate human review and applicable institutional procedures.

---

## Author

**Yuvraj Malethia**

B.Tech Computer Science & Engineering  
Thapar Institute of Engineering & Technology
