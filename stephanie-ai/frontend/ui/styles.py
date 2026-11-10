"""Stephanie.ai brand styling for the desktop client."""

from __future__ import annotations

# NoblePort palette — deep navy ground, brass accent, slate text.
NAVY = "#0B1F3A"
NAVY_LIGHT = "#13294B"
NAVY_PANEL = "#1B355F"
BRASS = "#C9A227"
BRASS_DARK = "#A8861C"
SLATE = "#C7D0DC"
SLATE_DIM = "#8794A6"
WHITE = "#F5F7FA"
GREEN = "#3FB37F"
AMBER = "#E0A83C"
RED = "#D9534F"

FONT_FAMILY = "Segoe UI"
TITLE_SIZE = 22
HEADING_SIZE = 16
BODY_SIZE = 13
SMALL_SIZE = 11

TRUTH_COLORS = {
    "LIVE": GREEN,
    "STAGED": AMBER,
    "ENFORCED": GREEN,
    "SIMULATED_ONLY": AMBER,
    "ERROR": RED,
}

BUTTON = {"fg_color": BRASS, "hover_color": BRASS_DARK, "text_color": NAVY}
BUTTON_SECONDARY = {"fg_color": NAVY_PANEL, "hover_color": NAVY_LIGHT, "text_color": WHITE}
PANEL = {"fg_color": NAVY_LIGHT, "corner_radius": 10}
SIDEBAR = {"fg_color": NAVY, "corner_radius": 0}


def apply_theme(ctk_module) -> None:
    """Configure CustomTkinter's global appearance for the Stephanie brand."""
    ctk_module.set_appearance_mode("dark")
    ctk_module.set_default_color_theme("dark-blue")


def font(ctk_module, size: int = BODY_SIZE, weight: str = "normal"):
    return ctk_module.CTkFont(family=FONT_FAMILY, size=size, weight=weight)
