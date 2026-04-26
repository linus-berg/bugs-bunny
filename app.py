import os
import tempfile
from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import PlainTextResponse
import pymupdf4llm

app = FastAPI(title="Document to Markdown Converter")

@app.post("/convert", response_class=PlainTextResponse)
async def convert_document(file: UploadFile):
    """
    Upload a document (e.g., PDF) and convert it to Markdown.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Create a temporary file to store the upload
    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Convert to markdown using pymupdf4llm
        md_text = pymupdf4llm.to_markdown(tmp_path)
        return md_text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Conversion failed: {str(e)}")
    finally:
        # Ensure the temporary file is removed
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.get("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
