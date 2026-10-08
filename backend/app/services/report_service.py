"""Plain-text validation report included in the ZIP download."""
from __future__ import annotations

import time

from ..utils.size_utils import UNIT_NOTE, format_bytes_exact, format_size


def build_report_text(spec: dict, status: dict) -> str:
    lines = [
        "AUDIT PDF VOLUME & ANNEXURE MANAGER - VALIDATION REPORT",
        "=" * 60,
        f"Generated:        {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Original PDF:     {format_size(spec['source_size'])} ({format_bytes_exact(spec['source_size'])}), "
        f"{spec['page_count']} pages",
        f"Size limit:       {format_size(spec['max_bytes']) if spec.get('max_bytes') else 'none'}",
        f"Volumes required: {spec.get('num_volumes_requested') or 'not specified'}",
        f"Units:            {UNIT_NOTE}",
        "",
    ]
    summary = status.get("summary") or {}
    for v in status.get("volumes", []):
        lines += [
            f"Volume {v['index']}: {v['filename']}",
            f"  Original pages:      {v['start_page']}–{v['end_page']} ({v['page_count']} pages)",
            f"  Sections:            " + "; ".join(
                s["title"] + (" [protected]" if s["protected"] else "") + (" [partial]" if s.get("partial") else "")
                for s in v["sections"]),
            f"  Final size:          {format_size(v.get('size_bytes'))} ({format_bytes_exact(v.get('size_bytes') or 0)})",
            f"  Compression:         Level {v.get('compression_level')} - {v.get('compression_label')}",
            f"  Searchable text:     {'yes' if v.get('text_preserved') else 'NO (pages rasterised)'}",
            f"  Status:              {v.get('status')}",
        ]
        for c in (v.get("validation") or {}).get("checks", []):
            mark = "PASS" if c["ok"] else ("WARN" if c["severity"] in ("warning", "limit") else "FAIL")
            lines.append(f"    [{mark}] {c['name']}: {c['detail']}")
        lines.append("")
    lines.append("Whole-document checks:")
    for c in summary.get("coverage", []):
        lines.append(f"    [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    lines += [
        "",
        f"Final total size: {format_size(summary.get('total_bytes'))}",
        f"Overall:          {'PASS' if summary.get('validation_passed') else 'FAIL'} - {summary.get('message', '')}",
    ]
    return "\n".join(lines) + "\n"
