import re

# Formatos soportados (los mismos que en el modal "Buscar Mineros" del programa central):
#   Sencillo: "192.168.10.100-175"     -> un solo rack, host 100 a 175
#   Rango:    "192.168.10-19.100-175"  -> racks 10 a 19, host 100 a 175 en cada uno
RANGE_PATTERN = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3}(?:-\d{1,3})?)\.(\d{1,3})-(\d{1,3})$")


def expand_range(text):
    text = text.strip()
    match = RANGE_PATTERN.match(text)
    if not match:
        raise ValueError(f"Rango invalido: {text}")

    o1, o2, o3_part, host_start, host_end = match.groups()
    if "-" in o3_part:
        o3_start, o3_end = (int(x) for x in o3_part.split("-"))
    else:
        o3_start = o3_end = int(o3_part)
    host_start, host_end = int(host_start), int(host_end)

    return [
        f"{o1}.{o2}.{o3}.{host}"
        for o3 in range(o3_start, o3_end + 1)
        for host in range(host_start, host_end + 1)
    ]


def expand_ranges(range_strings):
    ips = []
    for r in range_strings:
        ips.extend(expand_range(r))
    return ips
