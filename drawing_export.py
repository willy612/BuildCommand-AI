"""BuildCommand single-sheet PDFs. Originals are never modified.

Vector drawing content is kept. BuildCommand annotations are painted into the
reviewed copy. Native PDF comments, forms and article links are not copied.
"""
from io import BytesIO
import math
from pathlib import Path
import re


class ExportProblem(ValueError):
    pass


def dependencies_ready():
    try:
        import pypdf
        import reportlab
        return True
    except ImportError:
        return False


def export_fonts():
    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    root=Path(reportlab.__file__).parent/'fonts'
    for name,file in [('BCField','Vera.ttf'),('BCFieldBold','VeraBd.ttf'),('BCFieldItalic','VeraIt.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(root/file)))


def checked_annotations(items):
    if not isinstance(items, list) or len(items) > 2000:
        raise ExportProblem('This sheet has too many markups to export. Use a smaller saved layer.')
    for a in items:
        if not isinstance(a, dict) or a.get('type') not in {'pen','rect','cloud','arrow','text','stamp','measure'}:
            raise ExportProblem('A saved markup uses an unsupported tool. Export without markups or save a supported layer.')
        kind = a['type']
        keys = ('x','y','w','h') if kind in {'rect','cloud'} else ('x1','y1','x2','y2') if kind in {'arrow','measure'} else ('x','y') if kind in {'text','stamp'} else ()
        vals = [a.get(k) for k in keys]
        if kind == 'pen':
            pts = a.get('pts')
            if not isinstance(pts,list) or not 1 <= len(pts) <= 50000 or any(not isinstance(p,list) or len(p)!=2 for p in pts):
                raise ExportProblem('A pen markup could not be read. Save it again before exporting.')
            vals = [v for p in pts for v in p]
        if any(type(v) not in (float,int) or not math.isfinite(v) or not -1 <= v <= 2 for v in vals):
            raise ExportProblem('A saved markup has invalid coordinates.')
        if 'size' in a and (type(a['size']) not in (int,float) or not math.isfinite(a['size']) or not 6<=a['size']<=96):
            raise ExportProblem('A saved text markup has an invalid size.')
        if len(str(a.get('text',''))) > 4000 or len(str(a.get('label',''))) > 200:
            raise ExportProblem('A saved markup label is too long.')
    return items


def paint(canvas, width, height, items, pins):
    from reportlab.lib.colors import HexColor
    def xy(x,y): return x*width, (1-y)*height
    def arrow(a):
        x1,y1=xy(a['x1'],a['y1']);x2,y2=xy(a['x2'],a['y2'])
        canvas.line(x1,y1,x2,y2);angle=math.atan2(y2-y1,x2-x1)
        for d in (-math.pi/6,math.pi/6):canvas.line(x2,y2,x2-12*math.cos(angle+d),y2-12*math.sin(angle+d))
        return x2,y2
    for a in checked_annotations(items):
        color=a.get('color','#ff2d2d');color=color if isinstance(color,str) and re.fullmatch(r'#[0-9a-fA-F]{6}',color) else '#ff2d2d'
        canvas.setStrokeColor(HexColor(color));canvas.setFillColor(HexColor(color));canvas.setLineWidth(3)
        kind=a['type']
        if kind=='pen':
            path=canvas.beginPath();path.moveTo(*xy(*a['pts'][0]))
            for p in a['pts'][1:]:path.lineTo(*xy(*p))
            canvas.drawPath(path)
        elif kind in {'rect','cloud'}:
            x,y=xy(a['x'],a['y']);w=a['w']*width;h=a['h']*height
            if kind=='rect':canvas.rect(x,y-h,w,h)
            elif w>2 and h>2:
                r=max(6,min(16,min(w,h)/5));nx=max(2,math.ceil(w/(r*1.6)));ny=max(2,math.ceil(h/(r*1.6)))
                path=canvas.beginPath();path.moveTo(x,y);current=(x,y)
                def quad(cx,cy,ex,ey):
                    nonlocal current
                    sx,sy=current;path.curveTo(sx+2*(cx-sx)/3,sy+2*(cy-sy)/3,ex+2*(cx-ex)/3,ey+2*(cy-ey)/3,ex,ey);current=(ex,ey)
                for i in range(nx):quad(x+(i+.5)*w/nx,y+r,x+(i+1)*w/nx,y)
                for i in range(ny):quad(x+w+r,y-(i+.5)*h/ny,x+w,y-(i+1)*h/ny)
                for i in range(nx,0,-1):quad(x+(i-.5)*w/nx,y-h-r,x+(i-1)*w/nx,y-h)
                for i in range(ny,0,-1):quad(x-r,y-(i-.5)*h/ny,x,y-(i-1)*h/ny)
                path.close();canvas.drawPath(path)
        elif kind in {'arrow','measure'}:
            x,y=arrow(a)
            if kind=='measure':canvas.setFont('BCField',16);canvas.drawString(x+6,y+6,str(a.get('label','')))
        else:
            x,y=xy(a['x'],a['y']);size=a.get('size',22 if kind=='stamp' else 18);font='BCFieldBold' if kind=='stamp' else 'BCField'
            value=str(a.get('text') or ('FIELD VERIFY' if kind=='stamp' else 'Note')).replace('\n',' ')
            canvas.setFont(font,size)
            if kind=='stamp':canvas.rect(x-6,y-6,canvas.stringWidth(value,font,size)+12,size+12)
            canvas.drawString(x,y,value)
    for i,p in enumerate(pins,1):
        x,y=xy(p['x'],p['y']);radius=max(8,min(16,width/65))
        canvas.setStrokeColor(HexColor('#ffffff'));canvas.setFillColor(HexColor('#183d59'));canvas.setLineWidth(2)
        canvas.circle(x,y,radius,stroke=1,fill=1);canvas.setFillColor(HexColor('#ffffff'));canvas.setFont('BCFieldBold',radius)
        canvas.drawCentredString(x,y-radius*.35,str(i))


def make_sheet(path, page_number, annotations, pins, metadata):
    """Return one drawing page, plus a clearly labeled notes page when selected."""
    try:
        from pypdf import PdfReader, PdfWriter, Transformation
        from pypdf.generic import RectangleObject, NameObject
        from reportlab.pdfgen import canvas as pdfcanvas
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise ExportProblem('Sheet export needs pypdf and reportlab installed. Your administrator can install requirements-drawings-8-16-0.txt.') from exc
    path=Path(path)
    if path.stat().st_size>500*1024*1024:raise ExportProblem('The drawing exceeds the 500 MB export limit.')
    checked_annotations(annotations)
    try:
        export_fonts()
        with path.open('rb') as f:signature=f.read(8)
        if signature.startswith(b'%PDF-'):
            reader=PdfReader(str(path))
            if reader.is_encrypted:raise ExportProblem('Use an unlocked copy of this drawing for a sheet release.')
            if type(page_number) is not int or not 1<=page_number<=len(reader.pages):raise ExportProblem('The registered page does not exist in this PDF. Correct the drawing register first.')
            writer=PdfWriter()
            page=writer.add_page(reader.pages[page_number-1],excluded_keys=['/Annots','/B','/AA','/PieceInfo','/Metadata','/PresSteps'])
            if float(page.get('/UserUnit',1))!=1:raise ExportProblem('This PDF uses a nonstandard page scale. Save a standard PDF copy before releasing a sheet.')
            if page.rotation:page.transfer_rotation_to_content()
            left,bottom,right,top=map(float,page.cropbox);w=right-left;h=top-bottom
            page.add_transformation(Transformation().translate(-left,-bottom))
            for key in ('/MediaBox','/CropBox','/TrimBox','/BleedBox','/ArtBox'):page[NameObject(key)]=RectangleObject((0,0,w,h))
        else:
            if page_number!=1:raise ExportProblem('An image drawing has only one sheet.')
            from PIL import Image
            with Image.open(path) as im:
                if im.width*im.height>60_000_000:raise ExportProblem('Use an image below 60 megapixels for sheet export.')
                w,h=im.size;buf=BytesIO();cv=pdfcanvas.Canvas(buf,pagesize=(w,h));cv.drawImage(ImageReader(im.convert('RGB')),0,0,w,h);cv.save()
            writer=PdfWriter();page=writer.add_page(PdfReader(buf).pages[0])
        if not all(math.isfinite(n) and 36<=n<=14400 for n in (w,h)):raise ExportProblem('This drawing page has an unsupported size.')
        if annotations or pins:
            overlay=BytesIO();cv=pdfcanvas.Canvas(overlay,pagesize=(w,h));paint(cv,w,h,annotations,pins);cv.showPage();cv.save()
            page.merge_page(PdfReader(overlay).pages[0])
        if pins:
            from html import escape
            notes=BytesIO();styles=getSampleStyleSheet()
            for name in styles.byName:styles[name].fontName='BCFieldBold' if name.startswith('Heading') or name=='Title' else 'BCField'
            story=[Paragraph('BuildCommand AI | Field notes',styles['Title']),Paragraph(escape(metadata['label']),styles['Heading2']),Paragraph('The numbered pins refer to the drawing on page 1. These are the notes selected for this release.',styles['BodyText']),Spacer(1,18)]
            for i,p in enumerate(pins,1):
                story.extend([Paragraph('Pin '+str(i)+' | '+escape(p['title']),styles['Heading3']),Paragraph(escape(p['body']).replace('\n','<br/>'),styles['BodyText']),Spacer(1,12)])
            SimpleDocTemplate(notes,pagesize=(612,792),leftMargin=48,rightMargin=48,topMargin=48,bottomMargin=48).build(story)
            for p in PdfReader(notes).pages:writer.add_page(p)
        writer.add_metadata({'/Title':metadata['label'],'/Author':'BuildCommand AI','/Subject':'Reviewed single drawing sheet; original file preserved'})
        result=BytesIO();writer.write(result);data=result.getvalue()
        if len(data)>100*1024*1024:raise ExportProblem('The exported sheet exceeds 100 MB. Use a smaller source PDF.')
        return data
    except ExportProblem:raise
    except Exception as exc:
        raise ExportProblem('This sheet could not be exported. Check that the original is a valid PDF or image, then try again.') from exc
