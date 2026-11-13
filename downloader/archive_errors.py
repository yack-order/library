"""Controlled failures shared by script entry points and imported helpers."""


class ArchiveError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
