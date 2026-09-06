"""Minimal ezdxf-like entities shared by geometry unit tests."""


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __getitem__(self, index):
        return (self.x, self.y)[index]


class DxfLineAttributes:
    def __init__(self, start, end):
        self.start = Point(*start)
        self.end = Point(*end)


class Line:
    def __init__(self, start, end):
        self.dxf = DxfLineAttributes(start, end)

    def dxftype(self):
        return "LINE"
