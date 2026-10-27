"""PDF text (pdftotext, OCR fallback for scans) and image OCR (tesseract kor+eng, confidence kept)."""
import os
import re
import shutil
import subprocess
import tempfile

OCR_LANG = 'kor+eng'


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def pdf_text(data: bytes, max_pages: int = 8) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'in.pdf')
        open(p, 'wb').write(data)
        try:
            out = subprocess.run(['pdftotext', '-layout', '-l', str(max_pages), p, '-'], capture_output=True, timeout=60).stdout.decode('utf-8', 'replace')
        except Exception:
            out = ''
        pages = max(1, out.count('\f'))
        if len(re.sub(r'\s+', '', out)) >= 40:
            return {'text': out.replace('\f', '\n').strip(), 'method': 'pdf', 'pages': pages, 'ocr_conf': None}
        # scanned PDF: render pages and OCR (bounded)
        if have('pdftoppm') and have('tesseract'):
            subprocess.run(['pdftoppm', '-r', '150', '-l', str(min(3, max_pages)), '-png', p, os.path.join(d, 'pg')], capture_output=True, timeout=120)
            texts, confs = [], []
            for f in sorted(x for x in os.listdir(d) if x.startswith('pg') and x.endswith('.png')):
                r = ocr_image(open(os.path.join(d, f), 'rb').read())
                texts.append(r['text'])
                if r['ocr_conf'] is not None:
                    confs.append(r['ocr_conf'])
            return {'text': '\n'.join(texts).strip(), 'method': 'ocr', 'pages': len(texts), 'ocr_conf': (sum(confs) / len(confs)) if confs else None}
    return {'text': '', 'method': 'pdf', 'pages': 0, 'ocr_conf': None}


def ocr_image(data: bytes, lang: str = OCR_LANG) -> dict:
    """Returns text rebuilt line by line from tesseract TSV plus mean word confidence (0-100)."""
    if not have('tesseract'):
        return {'text': '', 'ocr_conf': None, 'method': 'ocr', 'error': 'tesseract_missing'}
    from io import BytesIO

    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        try:
            im = Image.open(BytesIO(data)).convert('L')
        except Exception:
            return {'text': '', 'ocr_conf': None, 'method': 'ocr', 'error': 'bad_image'}
        if im.width < 1400:
            k = 1400 / im.width
            im = im.resize((int(im.width * k), int(im.height * k)))
        p = os.path.join(d, 'in.png')
        im.save(p)
        r = subprocess.run(['tesseract', p, 'stdout', '-l', lang, '--psm', '6', 'tsv'], capture_output=True, timeout=120)
    lines: dict = {}
    confs = []
    for row in r.stdout.decode('utf-8', 'replace').splitlines()[1:]:
        c = row.split('\t')
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        lines.setdefault((c[2], c[3], c[4]), []).append(c[11].strip())
        confs.append(conf)
    text = '\n'.join(' '.join(w) for _, w in sorted(lines.items(), key=lambda kv: tuple(int(x) for x in kv[0])))
    return {'text': text.strip(), 'ocr_conf': (sum(confs) / len(confs)) if confs else None, 'method': 'ocr'}
