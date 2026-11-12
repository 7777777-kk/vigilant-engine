"""Word pagination safeguards for generated tables and mixed PDF pages."""
from docx.enum.section import WD_SECTION_START, WD_ORIENT
from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx.oxml.ns import qn
from docx.shared import Pt


def compact_paragraph(paragraph, empty=False):
    fmt = paragraph.paragraph_format
    fmt.keep_with_next = False
    fmt.keep_together = False
    fmt.widow_control = False
    fmt.page_break_before = False
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.line_spacing = Pt(1) if empty else 1.0
    if empty:
        # Empty section/table terminators must not inherit 11pt + 8pt spacing.
        from docx.oxml import OxmlElement
        paragraph_properties = paragraph._p.get_or_add_pPr()
        properties = paragraph_properties.find(qn('w:rPr'))
        if properties is None:
            properties = OxmlElement('w:rPr')
            paragraph_properties.insert_element_before(properties, 'w:sectPr', 'w:pPrChange')
        for name in ('w:sz', 'w:szCs'):
            node = properties.find(qn(name))
            if node is None:
                node = OxmlElement(name)
                properties.append(node)
            node.set(qn('w:val'), '2')
        for run in paragraph.runs:
            run.font.size = Pt(1)


def begin_page(document, sequence, source_section=None, size=None):
    section = document.sections[-1] if sequence == 0 else document.add_section(WD_SECTION_START.NEW_PAGE)
    if sequence:
        compact_paragraph(document.paragraphs[-1], empty=True)
    if source_section is not None:
        for key in ('page_width', 'page_height', 'orientation', 'top_margin', 'bottom_margin',
                    'left_margin', 'right_margin', 'header_distance', 'footer_distance', 'gutter'):
            setattr(section, key, getattr(source_section, key))
    else:
        width, height = size or (595.28, 841.89)
        section.orientation = WD_ORIENT.LANDSCAPE if width > height else WD_ORIENT.PORTRAIT
        section.page_width, section.page_height = Pt(width), Pt(height)
        section.top_margin = section.bottom_margin = Pt(min(30, height / 12))
        section.left_margin = section.right_margin = Pt(min(30, width / 12))
    # Source pages should never require an odd/even page (which inserts blanks).
    section.start_type = WD_SECTION_START.NEW_PAGE
    return section


def relax_table(table, usable_height=None, compact=False):
    for row in table.rows:
        properties = row._tr.get_or_add_trPr()
        for node in list(properties.findall(qn('w:cantSplit'))):
            properties.remove(node)
        if usable_height is not None and row.height and row.height > usable_height:
            # A row minimum taller than the printable page cannot be satisfied.
            for node in list(properties.findall(qn('w:trHeight'))):
                properties.remove(node)
        elif row.height_rule == WD_ROW_HEIGHT_RULE.EXACTLY:
            row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                if compact:
                    compact_paragraph(paragraph)
                else:
                    fmt = paragraph.paragraph_format
                    fmt.keep_with_next = False
                    fmt.keep_together = False
                    fmt.page_break_before = False
            for nested in cell.tables:
                relax_table(nested, usable_height, compact)


def finish_document(document):
    """Relax table pagination without deleting source blank pages or text."""
    from docx.table import Table
    section_index = 0
    for element in document.element.body:
        section = document.sections[min(section_index, len(document.sections)-1)]
        if element.tag == qn('w:tbl'):
            usable = section.page_height - section.top_margin - section.bottom_margin
            relax_table(Table(element, document._body), usable_height=usable)
        if element.tag == qn('w:p') and element.xpath('./w:pPr/w:sectPr'):
            section_index += 1
    # Reduce only terminal empty paragraphs, not deliberate spacers in a page.
    for element in reversed(list(document.element.body)[:-1]):
        if element.tag != qn('w:p') or element.xpath('.//w:t | .//w:drawing | .//w:object | .//w:fldChar'):
            break
        if element.xpath('./w:pPr/w:sectPr'):
            break
        from docx.text.paragraph import Paragraph
        paragraph = Paragraph(element, document._body)
        # A stale final hard break otherwise creates an entirely empty last page.
        for node in element.xpath('.//w:br[@w:type="page"]'):
            node.getparent().remove(node)
        compact_paragraph(paragraph, empty=True)
