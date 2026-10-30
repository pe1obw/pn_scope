"""Colours shared by the on-screen plot and the exported figures.
Categorical slots are assigned to traces in fixed order (colour follows the trace)."""
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
FLOOR = "#8a8984"        # reference / analyzer-floor trace (neutral, not a series colour)
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#c4c3bd"
AXIS = "#8a8984"  # on-screen axis; pyqtgraph also draws the grid in this colour
REGION = (42, 120, 214, 28)  # integration band fill (RGBA)
MARKER = "#0b0b0b"
