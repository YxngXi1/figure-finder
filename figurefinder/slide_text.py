"""Consistent caption and citation text boxes for Slides batch requests."""

from . import slide_layout


def text_box_requests(object_id, slide_id, box, text, font_size, link=None):
    x, y, w, h = box
    style = {"fontFamily": "Arial", "fontSize": {"magnitude": font_size, "unit": "PT"},
             "foregroundColor": {"opaqueColor": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}}}
    fields = "fontFamily,fontSize,foregroundColor"
    if link:
        style.update(link={"url": link}, underline=True)
        style["foregroundColor"]["opaqueColor"]["rgbColor"] = {"red": .15, "green": .3, "blue": .5}
        fields += ",link,underline"
    return [
        {"createShape": {"objectId": object_id, "shapeType": "TEXT_BOX", "elementProperties": {
            "pageObjectId": slide_id, "size": {"width": {"magnitude": w, "unit": "PT"},
            "height": {"magnitude": h, "unit": "PT"}}, "transform": {
                "scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"}}}},
        {"insertText": {"objectId": object_id, "text": "\n".join(slide_layout.caption_lines(text, w, font_size)),
                        "insertionIndex": 0}},
        {"updateTextStyle": {"objectId": object_id, "textRange": {"type": "ALL"},
                             "style": style, "fields": fields}},
        {"updateParagraphStyle": {"objectId": object_id, "textRange": {"type": "ALL"}, "style": {
            "alignment": "START", "spaceAbove": {"magnitude": 0, "unit": "PT"},
            "spaceBelow": {"magnitude": 0, "unit": "PT"}, "lineSpacing": 100},
            "fields": "alignment,spaceAbove,spaceBelow,lineSpacing"}},
        {"updateShapeProperties": {"objectId": object_id, "shapeProperties": {
            "contentAlignment": "TOP", "outline": {"propertyState": "NOT_RENDERED"},
            "shapeBackgroundFill": {"solidFill": {"color": {
                "rgbColor": {"red": 1, "green": 1, "blue": 1}}, "alpha": 1}}},
            "fields": "contentAlignment,outline,shapeBackgroundFill"}},
    ]
