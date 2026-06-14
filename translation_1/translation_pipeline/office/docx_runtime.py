"""DOCX 추출/주입/저장 런타임."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.nodes import is_translatable

from .runtime_common import _element_type_with_placeholder, _node_translation


def extract_docx(file_path: str) -> Tuple[Any, List[dict]]:
    """DOCX 문서에서 번역 가능한 텍스트 노드를 추출한다.

    Args:
        file_path: 입력 DOCX 경로.

    Returns:
        저장 컨텍스트와 노드 목록.
    """

    import zipfile

    from lxml import etree

    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"

    def is_inside_fallback(elem: Any) -> bool:
        parent = elem.getparent()
        while parent is not None:
            if parent.tag == f"{{{ns_mc}}}Fallback":
                return True
            parent = parent.getparent()
        return False

    def collect_t_from_runs(container: Any) -> List[Any]:
        t_nodes: List[Any] = []
        for child in container:
            tag = child.tag
            if tag == f"{{{ns_w}}}r":
                for sub in child:
                    if sub.tag == f"{{{ns_w}}}t":
                        t_nodes.append(sub)
            elif tag == f"{{{ns_w}}}hyperlink":
                for wr in child:
                    if wr.tag == f"{{{ns_w}}}r":
                        for sub in wr:
                            if sub.tag == f"{{{ns_w}}}t":
                                t_nodes.append(sub)
            elif tag == f"{{{ns_w}}}ins":
                for wr in child:
                    if wr.tag == f"{{{ns_w}}}r":
                        for sub in wr:
                            if sub.tag == f"{{{ns_w}}}t":
                                t_nodes.append(sub)
        return t_nodes

    def ancestor(element: Any, tag: str) -> Any | None:
        parent = element.getparent()
        while parent is not None:
            if parent.tag == tag:
                return parent
            parent = parent.getparent()
        return None

    def index_among(parent: Any, child: Any, tag: str) -> int | None:
        if parent is None:
            return None
        index = 0
        for item in parent:
            if item.tag != tag:
                continue
            if item is child:
                return index
            index += 1
        return None

    def docx_paragraph_style(wp: Any) -> str:
        p_pr = wp.find(f"{{{ns_w}}}pPr")
        if p_pr is None:
            return ""
        p_style = p_pr.find(f"{{{ns_w}}}pStyle")
        if p_style is None:
            return ""
        return str(p_style.get(f"{{{ns_w}}}val") or "")

    def is_list_paragraph(wp: Any) -> bool:
        p_pr = wp.find(f"{{{ns_w}}}pPr")
        return p_pr is not None and p_pr.find(f"{{{ns_w}}}numPr") is not None

    def docx_node_metadata(
        table_indices: dict[Any, int],
        wp: Any,
        source: str,
    ) -> dict[str, Any]:
        tc = ancestor(wp, f"{{{ns_w}}}tc")
        if tc is not None:
            tr = ancestor(tc, f"{{{ns_w}}}tr")
            tbl = ancestor(tr, f"{{{ns_w}}}tbl") if tr is not None else None
            row_index = index_among(tbl, tr, f"{{{ns_w}}}tr") if tbl is not None else None
            col_index = index_among(tr, tc, f"{{{ns_w}}}tc") if tr is not None else None
            return {
                "doc_format": "docx",
                "element_type": "table_cell",
                "group": "table_cell",
                "table_index": table_indices.get(tbl) if tbl is not None else None,
                "row_index": row_index,
                "col_index": col_index,
                "row": row_index,
                "col": col_index,
                "is_header": row_index == 0,
            }

        style = docx_paragraph_style(wp)
        element_type = "paragraph"
        if style.lower().startswith("heading"):
            element_type = "heading"
        elif is_list_paragraph(wp):
            element_type = "list_item"
        return {
            "doc_format": "docx",
            "element_type": element_type,
            "paragraph_style": style,
            "source": source,
        }

    def extract_from_tree(root: Any, source: str) -> List[dict]:
        found: List[dict] = []
        table_indices = {tbl: index for index, tbl in enumerate(root.iter(f"{{{ns_w}}}tbl"))}
        for wp in root.iter(f"{{{ns_w}}}p"):
            if is_inside_fallback(wp):
                continue
            metadata = docx_node_metadata(table_indices, wp, source)
            t_nodes = collect_t_from_runs(wp)
            text = "".join(t.text for t in t_nodes if t.text).strip()
            if text and is_translatable(text):
                node_metadata = {
                    **metadata,
                    "element_type": _element_type_with_placeholder(
                        text,
                        str(metadata.get("element_type") or "paragraph"),
                    ),
                }
                found.append(
                    {
                        "type": "xml_text",
                        "t_nodes": t_nodes,
                        "text": text,
                        "source": source,
                        **node_metadata,
                    }
                )
            for sdt in wp:
                if sdt.tag != f"{{{ns_w}}}sdt":
                    continue
                if is_inside_fallback(sdt):
                    continue
                sdt_content = sdt.find(f"{{{ns_w}}}sdtContent")
                if sdt_content is None:
                    continue
                sdt_t = collect_t_from_runs(sdt_content)
                sdt_text = "".join(t.text for t in sdt_t if t.text).strip()
                if sdt_text and is_translatable(sdt_text):
                    node_metadata = {
                        **metadata,
                        "element_type": _element_type_with_placeholder(
                            sdt_text,
                            str(metadata.get("element_type") or "paragraph"),
                        ),
                    }
                    found.append(
                        {
                            "type": "xml_text",
                            "t_nodes": sdt_t,
                            "text": sdt_text,
                            "source": source,
                            **node_metadata,
                        }
                    )

        for ap in root.iter(f"{{{ns_a}}}p"):
            if is_inside_fallback(ap):
                continue
            t_nodes = [t for t in ap.iter(f"{{{ns_a}}}t") if not is_inside_fallback(t)]
            text = "".join(t.text for t in t_nodes if t.text).strip()
            if text and is_translatable(text):
                found.append(
                    {
                        "type": "xml_text",
                        "t_nodes": t_nodes,
                        "text": text,
                        "source": source,
                        "doc_format": "docx",
                        "element_type": _element_type_with_placeholder(text, "text_box"),
                    }
                )
        return found

    nodes: List[dict] = []
    xml_parts: Dict[str, Any] = {}
    parser = etree.XMLParser(remove_blank_text=False)

    with zipfile.ZipFile(file_path, "r") as zip_file:
        doc_root = etree.fromstring(zip_file.read("word/document.xml"), parser)
        xml_parts["word/document.xml"] = doc_root
        nodes.extend(extract_from_tree(doc_root, "body"))

        for name in zip_file.namelist():
            if (name.startswith("word/header") or name.startswith("word/footer")) and name.endswith(".xml"):
                hf_root = etree.fromstring(zip_file.read(name), parser)
                xml_parts[name] = hf_root
                source = "header" if "header" in name else "footer"
                nodes.extend(extract_from_tree(hf_root, source))

    context = {"file_path": file_path, "xml_parts": xml_parts}
    log_info(f"[DOCX 추출] {len(nodes)}개 텍스트 추출 완료")
    return context, nodes



def inject_docx(context: Any, nodes: List[dict], trans_map: Dict[str, str]) -> None:
    """DOCX XML 텍스트 노드에 번역 결과를 주입한다.

    Args:
        context: DOCX 저장 컨텍스트.
        nodes: 번역 노드 목록.
        trans_map: 원문/번역 매핑.

    Returns:
        없음.
    """

    count = 0
    for node in nodes:
        original = str(node["text"])
        translated = _node_translation(node, trans_map)
        if not translated or translated == original:
            continue
        t_nodes = node["t_nodes"]
        if t_nodes:
            t_nodes[0].text = translated
            for item in t_nodes[1:]:
                item.text = ""
            count += 1
    log_info(f"[DOCX 주입] {count}개 노드 번역 적용")


def save_docx(context: Any, output_path: str) -> None:
    """DOCX 수정 XML을 다시 파일로 저장한다.

    Args:
        context: DOCX 저장 컨텍스트.
        output_path: 저장할 경로.

    Returns:
        없음.
    """

    import zipfile

    from lxml import etree

    file_path = context["file_path"]
    xml_parts = context["xml_parts"]
    with zipfile.ZipFile(file_path, "r") as source_zip:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target_zip:
            for item in source_zip.infolist():
                if item.filename in xml_parts:
                    xml_bytes = etree.tostring(
                        xml_parts[item.filename],
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
                    target_zip.writestr(item, xml_bytes)
                else:
                    target_zip.writestr(item, source_zip.read(item.filename))

