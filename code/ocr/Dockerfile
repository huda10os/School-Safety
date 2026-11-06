# Indra OCR (Datalab), CPU-only ONNX Runtime. Code Apache-2.0; model weights
# under a modified OpenRAIL-M licence (research use is fine; see the model card).
FROM python:3.13-slim
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN pip install --no-cache-dir indra-ocr==1.1.2
WORKDIR /work
