from __future__ import annotations

import io
import textwrap
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    normalized = (value or "").strip().lstrip("#")
    if len(normalized) != 6:
        return fallback
    try:
        return tuple(int(normalized[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return fallback


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    x: int,
    y: int,
    max_width: int,
    line_spacing: int = 10,
) -> int:
    words = text.split()
    if not words:
        return y
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    cursor_y = y
    for line in lines:
        draw.text((x, cursor_y), line, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), line, font=font)
        cursor_y += (bbox[3] - bbox[1]) + line_spacing
    return cursor_y


def _format_citation(citation: dict[str, Any]) -> str:
    label = str(citation.get("label") or citation.get("metricKey") or "Market signal")
    source = str(citation.get("sourceName") or "Source")
    as_of = str(citation.get("asOfDate") or "")
    return f"{label}: {source}, {as_of}".strip().strip(",")


def _shorten_text(value: str, width: int) -> str:
    return textwrap.shorten(" ".join(value.split()), width=width, placeholder="...")


class MarketContentImageService:
    @staticmethod
    def render_market_artifact_image(
        *,
        headline: str,
        summary: str,
        buyer_message: str,
        seller_message: str,
        citations: list[dict[str, Any]],
        primary_hex: str = "#2563EB",
        secondary_hex: str = "#0F172A",
    ) -> bytes:
        width = 1080
        height = 1350
        primary = _hex_to_rgb(primary_hex, (37, 99, 235))
        secondary = _hex_to_rgb(secondary_hex, (15, 23, 42))
        background = (244, 247, 251)
        card = (255, 255, 255)
        text = (15, 23, 42)
        muted = (71, 85, 105)

        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)

        for y in range(height):
            blend = y / max(height - 1, 1)
            row_color = tuple(
                round(background[index] * (1 - blend * 0.16) + primary[index] * (blend * 0.16))
                for index in range(3)
            )
            draw.line((0, y, width, y), fill=row_color)

        margin = 64
        draw.rounded_rectangle((margin, margin, width - margin, height - margin), radius=44, fill=card)
        draw.rounded_rectangle((margin, margin, width - margin, margin + 120), radius=44, fill=secondary)
        draw.rectangle((margin, margin + 62, width - margin, margin + 120), fill=secondary)

        tag_font = _load_font(34, bold=True)
        title_font = _load_font(72, bold=True)
        body_font = _load_font(35)
        section_font = _load_font(32, bold=True)
        foot_font = _load_font(24)

        draw.text((margin + 46, margin + 38), "MARKET INTELLIGENCE", font=tag_font, fill=(219, 234, 254))

        cursor_y = margin + 172
        cursor_y = _draw_wrapped_text(
            draw,
            textwrap.shorten(headline.strip() or "Market Update", width=96, placeholder="..."),
            title_font,
            text,
            margin + 52,
            cursor_y,
            width - (margin + 52) * 2,
            line_spacing=16,
        )

        cursor_y += 24
        cursor_y = _draw_wrapped_text(
            draw,
            _shorten_text(summary.strip(), 210),
            body_font,
            muted,
            margin + 52,
            cursor_y,
            width - (margin + 52) * 2,
            line_spacing=12,
        )

        panel_top = max(cursor_y + 42, 660)
        panel_gap = 24
        panel_width = (width - (margin + 52) * 2 - panel_gap) // 2
        panel_height = 260
        panel_x = margin + 52
        for index, (label, message) in enumerate((("BUYERS", buyer_message), ("SELLERS", seller_message))):
            left = panel_x + index * (panel_width + panel_gap)
            draw.rounded_rectangle(
                (left, panel_top, left + panel_width, panel_top + panel_height),
                radius=30,
                fill=(239, 246, 255) if index == 0 else (240, 253, 244),
                outline=(219, 234, 254) if index == 0 else (187, 247, 208),
                width=2,
            )
            draw.text((left + 28, panel_top + 28), label, font=section_font, fill=primary if index == 0 else (22, 101, 52))
            _draw_wrapped_text(
                draw,
                _shorten_text(message.strip(), 90),
                _load_font(25),
                text,
                left + 28,
                panel_top + 82,
                panel_width - 56,
                line_spacing=10,
            )

        footer_top = height - margin - 252
        draw.rounded_rectangle(
            (margin + 52, footer_top, width - margin - 52, height - margin - 52),
            radius=30,
            fill=(248, 250, 252),
            outline=(226, 232, 240),
            width=2,
        )
        draw.text((margin + 84, footer_top + 30), "Sources", font=section_font, fill=text)
        citation_y = footer_top + 82
        for citation in citations[:2]:
            citation_y = _draw_wrapped_text(
                draw,
                _format_citation(citation),
                foot_font,
                muted,
                margin + 84,
                citation_y,
                width - (margin + 84) * 2,
                line_spacing=8,
            )
        _draw_wrapped_text(
            draw,
            "General market information only; not financial advice.",
            foot_font,
            (100, 116, 139),
            margin + 84,
            height - margin - 104,
            width - (margin + 84) * 2,
            line_spacing=8,
        )

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
