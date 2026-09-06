import os, sys
if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
    dirs = [
        base,
        os.path.join(base, 'onnxruntime', 'capi'),
        os.path.dirname(sys.executable),
    ]
    for d in dirs:
        if os.path.isdir(d):
            os.add_dll_directory(d)