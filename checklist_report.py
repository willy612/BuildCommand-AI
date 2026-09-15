"""Printable checklist snapshots, using the existing reportlab dependency."""
from io import BytesIO
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape


def build_report(snapshot, photo_path):
    from PIL import Image as PILImage
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, KeepTogether
    from drawing_export import export_fonts
    export_fonts()
    stream=BytesIO();width=468
    styles={
        'title':ParagraphStyle('Title',fontName='BCFieldBold',fontSize=23,leading=28,textColor=colors.HexColor('#15334b'),spaceAfter=12),
        'heading':ParagraphStyle('Heading',fontName='BCFieldBold',fontSize=13,leading=18,textColor=colors.HexColor('#15334b'),spaceBefore=15,spaceAfter=7),
        'body':ParagraphStyle('Body',fontName='BCField',fontSize=10,leading=15,spaceAfter=7),
        'small':ParagraphStyle('Small',fontName='BCField',fontSize=8,leading=12,textColor=colors.HexColor('#52667b'),spaceAfter=6),
    }
    def para(value,style='body'):
        return Paragraph(escape(str(value or '')).replace('\n','<br/>'),styles[style])
    run=snapshot['run'];story=[para(run['title'],'title'),para(snapshot['project']['name'],'heading'),
        para(run['kind'].title()+' checklist | '+run['check_date']+' | '+(run['area'] or 'Area not specified')),
        para('Prepared for review: '+datetime.fromisoformat(snapshot['prepared_at']).strftime('%b %d, %Y at %H:%M UTC')+'\nReviewer: '+snapshot['reviewer'],'small'),
        para('This records the project team\'s observations and review. It is not a regulatory inspection certificate or an electronic signature.','small')]
    if snapshot.get('template'):
        t=snapshot['template'];story.append(para('Company template: '+t['name']+' | version '+str(t['version']),'small'))
    buffers=[]
    for n,item in enumerate(snapshot['items'],1):
        story.append(KeepTogether([para(str(n)+'. '+item['label'],'heading'),para('Recorded response: '+snapshot['labels'][item['result']])]))
        if item['note']:story.append(para('Observation: '+item['note']))
        if item['was_attention']:story.append(para('First finding: '+item['finding_note'],'small'))
        if item['resolution']:story.append(para('Correction / verification: '+item['resolution']))
        correction=item.get('correction')
        if correction:
            story.append(para('Correction evidence record #'+str(correction['document']['id']),'small'))
            for r in correction['requests']:
                story.append(para('Request #'+str(r['id'])+' | '+('Access revoked' if r['revoked_at'] else r['state'])+' | '+str(r['recipient'] or 'Former account'),'small'))
            if correction['file']:
                f=correction['file'];story.append(para('Evidence file: '+f['original_name']+' | version '+str(f['revision'])+' | retained in Documents','small'))
        for photo in item['photos']:
            # Decode only validated raster files; embed a bounded thumbnail.
            with PILImage.open(photo_path(photo)) as source:
                thumb=source.convert('RGB');thumb.thumbnail((1200,900))
                raw=BytesIO();thumb.save(raw,format='JPEG',quality=82);raw.seek(0);buffers.append(raw)
                w,h=thumb.size;scale=min(width/w,220/h,1)
                im=Image(raw,width=w*scale,height=h*scale);im.hAlign='LEFT'
            story.append(KeepTogether([Spacer(1,7),para('Photo evidence - item '+str(n),'small'),im,para(photo['original_name']+' | '+(photo['caption'] or 'Photo evidence'),'small')]))
    story+=[Spacer(1,14),para('Review note','heading'),para(snapshot['review_note'] or 'No additional review note.'),
            para('The exact snapshot is filed only after approval in BuildCommand AI. Original photos and change history are retained with the checklist.','small')]
    def frame(canvas,doc):
        canvas.saveState();canvas.setFillColor(colors.HexColor('#15334b'));canvas.setFont('BCFieldBold',10)
        canvas.drawString(54,756,'BuildCommand AI');canvas.setStrokeColor(colors.HexColor('#d9a13b'));canvas.line(54,744,558,744)
        canvas.setFont('BCField',8);canvas.setFillColor(colors.HexColor('#52667b'))
        canvas.drawString(54,30,'Built By Willy LaHood ©2026');canvas.drawRightString(558,30,'Page '+str(doc.page));canvas.restoreState()
    SimpleDocTemplate(stream,pagesize=(612,792),leftMargin=72,rightMargin=72,topMargin=66,bottomMargin=56,
                      title=run['title'],author='BuildCommand AI').build(story,onFirstPage=frame,onLaterPages=frame)
    return stream.getvalue()
