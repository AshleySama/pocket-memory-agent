Optional local models

This lightweight release intentionally does not contain any model weights.
Place model folders beside PocketMemory.exe as follows:

models\Qwen3-4B\Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf
    Enables intelligent Q&A and automatic organization.

models\bge-small-zh-v1.5\
    Enables semantic and hybrid search. Copy the complete folder, including
    tokenizer.json and onnx\model_quantized.onnx.

models\rapidocr\
    Enables image OCR. Copy the complete folder with its four OCR files.

The application checks these folders locally and does not download anything.
