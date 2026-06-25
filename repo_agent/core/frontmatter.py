from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FrontmatterResult:
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(content: str) -> FrontmatterResult:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    end_idx = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_idx = index
            break
    if end_idx == -1:
        return FrontmatterResult(body=content)

    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip()

    return FrontmatterResult(meta=meta, body="\n".join(lines[end_idx + 1 :]).strip())


def format_frontmatter(meta: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {str(value).replace(chr(10), ' ').strip()}")
    lines.append("---")
    lines.append(body.strip())
    return "\n".join(lines).rstrip() + "\n"
