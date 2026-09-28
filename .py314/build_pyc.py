import sys, os, marshal, struct, time, importlib.util

SRC = os.environ.get("CAVA_SRC", r"C:\Users\clark\.config\yasb\cava_menu.py")
OUT = os.environ.get("CAVA_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cava_menu.pyc"))

with open(SRC, "r", encoding="utf-8") as f:
    source = f.read()

# Compile to a code object WITHOUT executing imports (imports resolve at runtime).
code = compile(source, "cava_menu.py", "exec")

magic = importlib.util.MAGIC_NUMBER          # Python 3.14 -> 0x0e2b
flags = 0                                     # 0 = timestamp/source-mode pyc (as YASB uses)
src_bytes = source.encode("utf-8")
size = len(src_bytes)
mtime = int(time.time())
bitfield = (mtime << 32) | size

header = magic + struct.pack("<I", flags) + struct.pack("<Q", bitfield)
data = header + marshal.dumps(code)

with open(OUT, "wb") as f:
    f.write(data)

print("Wrote", OUT, len(data), "bytes | magic", magic.hex())
