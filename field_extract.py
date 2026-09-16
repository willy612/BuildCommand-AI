"""Bounded subprocess for local PDF text and optional OCR. No network calls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def extract(path,ocr=False):
    path=Path(path)
    if path.stat().st_size>30*1024*1024:raise ValueError('Use a PDF of 30 MB or less per indexing pass.')
    if path.suffix.lower()=='.txt':return [dict(page=1,text=path.read_text(encoding='utf-8',errors='replace')[:180000],method='text')]
    from pypdf import PdfReader
    reader=PdfReader(path)
    if reader.is_encrypted:raise ValueError('Upload an unlocked PDF.')
    if len(reader.pages)>60:raise ValueError('Split this PDF into sections of up to 60 pages before indexing.')
    result=[]
    for n,page in enumerate(reader.pages,1):
        text=page.extract_text() or '';method='PDF text'
        if len(text.strip())<30 and ocr:
            if not shutil.which('pdftoppm') or not shutil.which('tesseract'):raise ValueError('OCR requires pdftoppm and tesseract on the server.')
            with tempfile.TemporaryDirectory(prefix='bc-ocr-') as folder:
                target=str(Path(folder)/'page')
                subprocess.run(['pdftoppm','-f',str(n),'-l',str(n),'-scale-to','2200','-singlefile','-png',str(path),target],check=True,timeout=20,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                proc=subprocess.run(['tesseract',target+'.png','stdout'],check=True,timeout=20,capture_output=True,text=True)
                text=proc.stdout;method='OCR — verify against original'
        result.append(dict(page=n,text=text[:12000],method=method))
    return result

if __name__=='__main__':
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU,(100,100))
            resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))
        except ImportError:pass
        print(json.dumps(dict(pages=extract(sys.argv[1],len(sys.argv)>2 and sys.argv[2]=='ocr'))))
    except Exception as exc:
        # No file paths or document contents in error output.
        print(json.dumps(dict(error=str(exc) if isinstance(exc,ValueError) else 'Extraction could not finish. Check the PDF or use a smaller file.')));sys.exit(1)
