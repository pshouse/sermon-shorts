# Bundled models

- `face_detection_yunet_2023mar.onnx` — YuNet face detector from the
  [OpenCV model zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet),
  MIT licensed (see `LICENSE.yunet`, © Shiqi Yu). Loaded through
  `cv2.FaceDetectorYN`, which needs OpenCV 4.5.4+; this static-shape file is
  the one OpenCV 4.x expects.
