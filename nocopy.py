#!/usr/bin/env python3
import argparse
import hashlib
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

CHUNK_SIZE = 8192

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
CONFLICT_COLOR = "\033[93m"

SEG_COLORS = [
    "\033[91m", "\033[92m", "\033[93m", "\033[94m",
    "\033[95m", "\033[96m", "\033[97m", "\033[31m",
    "\033[32m", "\033[33m", "\033[34m", "\033[35m",
    "\033[36m", "\033[37m", "\033[90m", "\033[91m",
]
def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def human_size(num):
    if num is None:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024 or unit == "TB":
            if unit == "B":
                return "{}{}".format(num, unit)
            return "{:.1f}{}".format(num, unit)
        num /= 1024.0


def human_speed(bps):
    if bps is None:
        return "?"
    for unit in ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"]:
        if bps < 1024 or unit == "TB/s":
            if unit == "B/s":
                return "{}{}".format(bps, unit)
            return "{:.1f}{}".format(bps, unit)
        bps /= 1024.0


def format_time(sec):
    if sec < 60:
        return "{:.2f}s".format(sec)
    if sec < 3600:
        m, s = divmod(int(sec), 60)
        return "{}m {}s".format(m, s)
    if sec < 86400:
        h, rem = divmod(int(sec), 3600)
        m = rem // 60
        return "{}h {}m".format(h, m)
    d = sec / 86400.0
    return "{:.2f}d".format(d)


class ProgressBars:
    """Single common progress bar aggregated across concurrent workers.

    All workers contribute to one shared bar. Drawing is serialized through a
    lock and throttled to avoid excessive terminal writes. Only one bar is
    shown regardless of the number of worker threads.
    """

    def __init__(self, bar_width=None, throttle=0.05):
        self.bar_width = bar_width
        self.throttle = throttle
        self.lock = threading.Lock()
        self.slots = []
        self.count = 0
        self.n_show = 0
        self.started = False
        self.last_draw = 0.0
        self.color = False

    def begin(self, slots):
        with self.lock:
            self.slots = list(slots)
            self.count = len(self.slots)
            self.n_show = self.count
            if self.bar_width is None:
                total = slots[0]["total"] if slots else 0
                cw = len(str(total))
                self.bar_width = max(1, 80 - (2 * cw + 13))
            self.started = False
            self.last_draw = 0.0

    def _line(self):
        total = self.slots[0]["total"] if self.slots else 0
        done = sum(s["cur"] for s in self.slots)
        if total:
            filled = int(self.bar_width * done / total)
            pct = 100.0 * done / total
        else:
            filled = self.bar_width
            pct = 100.0
        seg = []
        if self.count:
            curs = [s["cur"] for s in self.slots]
            tc = sum(curs) or 1
            seg = [int(filled * c / tc) for c in curs]
            rem = filled - sum(seg)
            i = 0
            while rem > 0:
                seg[i % self.count] += 1
                rem -= 1
                i += 1
            while rem < 0:
                big = max(range(self.count), key=lambda k: seg[k])
                seg[big] -= 1
                rem += 1
        parts = []
        for i in range(self.count):
            n = seg[i]
            if n <= 0:
                continue
            block = "#" * n
            if self.color:
                block = SEG_COLORS[i % len(SEG_COLORS)] + block + RESET
            parts.append(block)
        bar = "".join(parts) + "-" * (self.bar_width - filled)
        cw = len(str(total))
        return "  [{:>{}}/{}] [{}] {:.0f}%".format(
            done, cw, total, bar, pct)

    def _worker_lines(self):
        lines = []
        for i in range(self.n_show):
            s = self.slots[i]
            label = "#{}".format(i + 1)
            if self.color:
                label = SEG_COLORS[i % len(SEG_COLORS)] + label + RESET
            text = "    {} [{}]".format(label, s["cur"])
            text += " " + human_size(s.get("bytes", 0))
            if s.get("done"):
                start = s.get("start")
                end = s.get("end")
                if start is not None and end is not None and end > start:
                    bps = s.get("bytes", 0) / (end - start)
                    text += " средняя скорость " + human_speed(bps)
            else:
                start = s.get("start")
                if start is not None:
                    elapsed = time.monotonic() - start
                    bps = (s.get("bytes", 0) / elapsed) if elapsed > 0 else 0
                    text += " " + human_speed(bps)
                current = s.get("current")
                if current is not None:
                    text += " " + current
            lines.append(text)
        return lines

    def _block_lines(self):
        return [self._line()] + self._worker_lines()

    def _draw_plain(self):
        if self.started:
            sys.stdout.write("\r")
        sys.stdout.write(self._line())
        sys.stdout.flush()

    def _reserve_space(self, h):
        for _ in range(h):
            sys.stdout.write("\n")
        sys.stdout.write("\033[{}A".format(h))
        sys.stdout.flush()

    def _draw_ansi(self, lines):
        h = len(lines)
        if self.started and h > 1:
            sys.stdout.write("\033[{}A".format(h - 1))
            sys.stdout.write("\r")
        elif self.started:
            sys.stdout.write("\r")
        for i, line in enumerate(lines):
            sys.stdout.write("\033[K")
            sys.stdout.write(line)
            sys.stdout.write("\033[K")
            if i < h - 1:
                sys.stdout.write("\033[E")
        sys.stdout.flush()

    def draw(self, force=False):
        if not self.count:
            return
        with self.lock:
            if not force and time.time() - self.last_draw < self.throttle:
                return
            self.last_draw = time.time()
            if self.color:
                lines = self._block_lines()
                if not self.started:
                    self._reserve_space(len(lines))
                self._draw_ansi(lines)
            else:
                self._draw_plain()
            self.started = True

    def end(self):
        self.draw(force=True)
        with self.lock:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.started = False
            self.count = 0
            self.slots = []


class WorkQueue:
    """Thread-safe shared work queue for dynamic work-stealing.

    Workers pull the next item on demand; once a worker is done it immediately
    takes whatever work remains, so finished threads never sit idle while
    others still have pending items.
    """

    def __init__(self, items):
        self.items = list(items)
        self.n = len(self.items)
        self.next = 0
        self.lock = threading.Lock()

    def next_item(self):
        with self.lock:
            if self.next >= self.n:
                return None
            idx = self.next
            self.next += 1
            return idx, self.items[idx]


def _index_worker(wq, slot, pb):
    results = []
    while True:
        item = wq.next_item()
        if item is None:
            break
        idx, (full, rel) = item
        slot["current"] = rel
        if slot["start"] is None:
            slot["start"] = time.monotonic()
        size = file_size(full)
        digest = None
        if size is not None:
            digest = md5_of_file(full)
            slot["bytes"] = slot.get("bytes", 0) + size
        slot["cur"] += 1
        pb.draw()
        results.append((idx, rel, size, digest))
    slot["current"] = None
    slot["done"] = True
    slot["end"] = time.monotonic()
    return results


def _search_worker(shared_args, wq, slot, pb):
    results = []
    while True:
        item = wq.next_item()
        if item is None:
            break
        idx, (full, rel) = item
        slot["current"] = rel
        if slot["start"] is None:
            slot["start"] = time.monotonic()
        res = classify_source_file(shared_args, full, rel)
        results.append((idx, res))
        if res[4]:
            slot["bytes"] = slot.get("bytes", 0) + res[4]
        slot["cur"] += 1
        pb.draw()
    slot["current"] = None
    slot["done"] = True
    slot["end"] = time.monotonic()
    return results


def md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root):
    def walk(root_path, real_chain):
        real_root = os.path.realpath(root_path)
        if real_root in real_chain:
            return
        chain = real_chain | {real_root}
        try:
            entries = os.scandir(root_path)
        except OSError:
            return
        with entries:
            for entry in entries:
                if os.path.isdir(entry.path):
                    yield from walk(entry.path, chain)
                else:
                    full = os.path.join(root_path, entry.name)
                    rel = os.path.relpath(full, root)
                    yield full, rel

    yield from walk(root, frozenset())


def unique_target_path(target_root, rel):
    candidate = os.path.join(target_root, rel)
    if not os.path.lexists(candidate):
        return candidate
    base, ext = os.path.splitext(rel)
    n = 1
    while True:
        new_rel = "{}_{}".format(base, n) + ext
        candidate = os.path.join(target_root, new_rel)
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def build_index(target_root, color=False):
    files = list(iter_files(target_root))
    total = len(files)
    print("Сканирование целевого каталога ({})...".format(target_root))
    if total == 0:
        print("В целевом каталоге файлов не найдено.")
        return {}
    index = {}
    max_workers = min(32, os.cpu_count() or 1)
    wq = WorkQueue(files)
    pb = ProgressBars()
    slots = [{"total": total, "cur": 0, "current": None, "bytes": 0, "start": None}
             for _ in range(max_workers)]
    pb.color = color
    pb.begin(slots)
    indexed = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_index_worker, wq, slots[i], pb)
                   for i in range(max_workers)]
        for fut in futures:
            indexed.extend(fut.result())
    indexed.sort(key=lambda x: x[0])
    for idx, rel, size, digest in indexed:
        if size is None:
            continue
        index.setdefault(size, {}).setdefault(digest, []).append(os.path.join(target_root, rel))
    pb.end()
    if total:
        print()
        print("Индекс целевого каталога создан ({} файл(ов)).".format(total))
    return index


def render_table(rows, headers):
    ansi_re = re.compile(r"\033\[[0-9;]*m")

    def visible_len(s):
        return len(ansi_re.sub("", str(s)))

    cell_lines = [[str(c).split("\n") for c in r] for r in rows]
    widths = []
    for i, header in enumerate(headers):
        w = len(header)
        for r in cell_lines:
            for line in r[i]:
                w = max(w, visible_len(line))
        widths.append(w)

    def border(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def top_line():
        return border("┌", "┬", "┐")

    def sep_line():
        return border("├", "┼", "┤")

    def bottom_line():
        return border("└", "┴", "┘")

    def render_cell(c, width):
        parts = []
        for text in c:
            pad = width - visible_len(text)
            parts.append(" " + text + " " * pad + " ")
        return parts

    def row(cell_blocks):
        max_lines = max(len(b) for b in cell_blocks)
        parts = []
        for i, block in enumerate(cell_blocks):
            block = block + [""] * (max_lines - len(block))
            parts.append(render_cell(block, widths[i]))
        lines = []
        for z in range(max_lines):
            lines.append("│" + "│".join(p[z] for p in parts) + "│")
        return lines

    out = [top_line(), row([[h] for h in headers])[0], sep_line()]
    for r in cell_lines:
        out.extend(row(r))
    out.append(bottom_line())
    return "\n".join(out)


def classify_source_file(args, full, rel):
    index, target, intermediate, color, do_copy = args
    size = file_size(full)
    matches = []
    if size is not None:
        by_digest = index.get(size)
        if by_digest:
            digest = md5_of_file(full)
            matches = by_digest.get(digest, [])

    if matches:
        n = len(matches)
        if n == 1:
            if color:
                displays = ["   " + RED + os.path.relpath(matches[0], target) + RESET]
            else:
                displays = ["   " + os.path.relpath(matches[0], target)]
        else:
            displays = []
            for i, existing in enumerate(matches):
                if i == 0:
                    prefix = "┌╼ "
                elif i == n - 1:
                    prefix = "└╼ "
                else:
                    prefix = "├╼ "
                path = os.path.relpath(existing, target)
                if color:
                    displays.append(RED + prefix + path + RESET)
                else:
                    displays.append(prefix + path)
        status_display = (RED + "SKIP" + RESET) if color else "SKIP"
        return ("SKIP", status_display, rel, human_size(size), size, "", "\n".join(displays), None)

    orig_dest = os.path.join(target, rel)
    tgt_dest = unique_target_path(target, rel)
    conflict = tgt_dest != orig_dest
    inter_conflict = False
    if intermediate:
        dest = os.path.join(intermediate, rel)
        inter_conflict = os.path.lexists(dest)
    else:
        dest = tgt_dest

    if do_copy:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
        except OSError as e:
            status_display = "ERR"
            if intermediate:
                inter_display = os.path.relpath(dest, intermediate)
                if inter_conflict and color:
                    inter_display = CONFLICT_COLOR + inter_display + RESET
            else:
                inter_display = ""
            if conflict:
                if color:
                    target_display = "   " + CONFLICT_COLOR + rel + RESET + "\n" + "   " + CONFLICT_COLOR + "└╼ " + os.path.relpath(tgt_dest, target) + RESET
                else:
                    target_display = "   " + rel + "\n" + "   └╼ " + os.path.relpath(tgt_dest, target)
            else:
                target_display = "  " + os.path.relpath(tgt_dest, target)
            return ("ERR", "ERR", rel, human_size(size), size, inter_display, target_display, str(e))

    status_display = (GREEN + "COPY" + RESET) if color else "COPY"
    if intermediate:
        inter_display = os.path.relpath(dest, intermediate)
        if inter_conflict and color:
            inter_display = CONFLICT_COLOR + inter_display + RESET
    else:
        inter_display = ""
    if conflict:
        if color:
            target_display = "   " + CONFLICT_COLOR + rel + RESET + "\n" + "   " + CONFLICT_COLOR + "└╼ " + os.path.relpath(tgt_dest, target) + RESET
        else:
            target_display = "   " + rel + "\n" + "   └╼ " + os.path.relpath(tgt_dest, target)
    else:
        target_display = "   " + os.path.relpath(tgt_dest, target)
    return ("COPY", status_display, rel, human_size(size), size, inter_display, target_display, None)


def main():
    start_time = time.time()
    parser = argparse.ArgumentParser(
        prog="nocopy",
        description="Копирование файлов из источника в целевой каталог "
                    "с пропуском файлов, уже существующих в целевом каталоге "
                    "(по md5, вне зависимости от местоположения файла в подкаталогах целевого каталога).",
    )
    parser.add_argument("source", help="исходный каталог")
    parser.add_argument("target", help="целевой каталог")
    parser.add_argument("intermediate", nargs="?", default=None,
                        help="промежуточный каталог (файлы копируются в него вместо целевого, "
                        "но проверка существования всё равно выполняется в целевом)")
    parser.add_argument("-e", "--exec", action="store_true",
                        help="выполнить копирование (без опции — только проверка и отчёт)")
    parser.add_argument("-c", "--copy", action="store_true",
                        help="показывать в отчёте только файлы, которые будут скопированы")
    parser.add_argument("-s", "--skip", action="store_true",
                        help="показывать в отчёте только файлы, которые не будут скопированы (уже есть в целевом каталоге)")
    index_group = parser.add_mutually_exclusive_group()
    index_group.add_argument("-i", "--save-index", action="store_true",
                        help="использовать готовый индексный файл .<target>.nocopy из текущего каталога (не создавая его)")
    index_group.add_argument("-I", "--rebuild-index", action="store_true",
                        help="пересоздать файл индекса, даже если он уже существует")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    target = os.path.abspath(args.target)
    intermediate = os.path.abspath(args.intermediate) if args.intermediate else None

    if not os.path.isdir(source):
        sys.exit("Ошибка: исходный каталог не найден: {}".format(source))
    if os.path.exists(target) and not os.path.isdir(target):
        sys.exit("Ошибка: целевой путь существует и не является каталогом: {}".format(target))
    if intermediate and os.path.exists(intermediate) and not os.path.isdir(intermediate):
        sys.exit("Ошибка: промежуточный путь существует и не является каталогом: {}".format(intermediate))

    color = sys.stdout.isatty()

    print("Исходный каталог: {}".format(source))
    print("Целевой каталог:  {}".format(target))
    if intermediate:
        print("Промежуточный каталог: {}".format(intermediate))
    if args.copy and args.skip:
        report_note = "все файлы"
    elif args.skip:
        report_note = "только уже существующие"
    elif args.copy:
        report_note = "только копируемые"
    else:
        report_note = "не выводится (задайте -c и/или -s)"

    print("Режим: {} ({})".format(
        "копирование" if args.exec else "проверка",
        "копирование будет выполнено" if args.exec else "без копирования",
    ))
    print("Отчёт: {}".format(report_note))
    print()

    target_name = target.lstrip(os.sep).replace(os.sep, "_").replace(" ", "_")
    index_path = os.path.join(os.getcwd(), ".{}.nocopy".format(target_name))
    idx_start = time.time()
    if args.save_index:
        if not os.path.exists(index_path):
            sys.exit("Ошибка: файл индекса не найден: {} (запустите с -I, чтобы создать его)".format(index_path))
        print("Чтение индекса из {}".format(index_path))
        try:
            index = {}
            with open(index_path, "r") as f:
                for line in f:
                    parts = line.strip().split(" ", 2)
                    if len(parts) != 3:
                        continue
                    size, digest, p = parts
                    index.setdefault(int(size), {}).setdefault(digest, []).append(p)
        except (OSError, ValueError) as e:
            print("Ошибка чтения индекса: {}".format(e))
            index = {}
        print()
    else:
        index = build_index(target, color)
        if args.rebuild_index:
            try:
                with open(index_path, "w") as f:
                    for size, by_digest in index.items():
                        for digest, paths in by_digest.items():
                            for p in paths:
                                f.write("{} {} {}\n".format(size, digest, p))
                print("Индекс сохранён в {}".format(index_path))
            except OSError as e:
                print("Ошибка сохранения индекса: {}".format(e))
        print()
    idx_time = time.time() - idx_start

    rows = []
    no_count = 0
    ok_count = 0
    no_bytes = 0
    ok_bytes = 0
    errored = []

    search_start = time.time()
    source_files = list(iter_files(source))
    source_total = len(source_files)
    print("Поиск дублей в источнике ({})...".format(source))

    shared_args = (index, target, intermediate, color, args.exec)
    max_workers = min(32, os.cpu_count() or 1)
    wq = WorkQueue(source_files)
    pb = ProgressBars()
    slots = [{"total": source_total, "cur": 0, "current": None, "bytes": 0, "start": None}
             for _ in range(max_workers)]
    pb.color = color
    pb.begin(slots)
    collected = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_search_worker, shared_args, wq, slots[i], pb)
                   for i in range(max_workers)]
        for fut in futures:
            collected.extend(fut.result())
    pb.end()
    collected.sort(key=lambda x: x[0])
    ordered = [res for _, res in collected]

    for status, status_display, rel, size_str, size, inter_display, target_display, err in ordered:
        if status == "SKIP":
            rows.append(("SKIP", status_display, rel, size_str,
                         "", target_display))
            no_count += 1
            no_bytes += size or 0
        elif status == "ERR":
            errored.append((rel, err))
            rows.append(("ERR", "ERR", rel, size_str,
                         inter_display, target_display))
        else:
            rows.append(("COPY", status_display, rel, size_str,
                         inter_display, target_display))
            ok_count += 1
            ok_bytes += size or 0

    if source_total:
        print()
        print("Поиск дублей завершён ({} файл(ов) источника).".format(source_total))
    print()
    search_time = time.time() - search_start

    if args.copy and args.skip:
        report_rows = rows
    elif args.skip:
        report_rows = [r for r in rows if r[0] == "SKIP"]
    elif args.copy:
        report_rows = [r for r in rows if r[0] == "COPY"]
    else:
        report_rows = None

    if report_rows is not None:
        if intermediate:
            report_rows = [(r[1], r[2], r[3], r[5], r[4]) for r in report_rows]
            headers = ["копирование", "исходный файл", "размер", "целевой файл", "промежуточный файл"]
        else:
            report_rows = [(r[1], r[2], r[3], r[5]) for r in report_rows]
            headers = ["копирование", "исходный файл", "размер", "целевой файл"]
        print(render_table(report_rows, headers))
        print()

    all_bytes = no_bytes + ok_bytes
    plain_copy = "Будет скопировано (COPY)"
    plain_skip = "Уже есть в целевом (SKIP)"
    label_w = max(len("Всего файлов в источнике"),
                  len(plain_skip),
                  len(plain_copy))
    print("{:<{}}: {} ({})".format("Всего файлов в источнике", label_w, len(rows), human_size(all_bytes)))
    copy_line = "{}: {} ({})".format(plain_copy.ljust(label_w), ok_count, human_size(ok_bytes))
    skip_line = "{}: {} ({})".format(plain_skip.ljust(label_w), no_count, human_size(no_bytes))
    if color:
        print(GREEN + copy_line + RESET)
        print(RED + skip_line + RESET)
    else:
        print(copy_line)
        print(skip_line)

    if args.exec:
        copied = ok_count
        print("Скопировано: {}".format(copied))
        print("Ошибок: {}".format(len(errored)))
        for rel, err in errored:
            print("  {}: {}".format(rel, err))
    else:
        print((YELLOW + "Фактическое копирование не выполнялось (запустите с -e)." + RESET) if color else "Фактическое копирование не выполнялось (запустите с -e).")

    total_time = time.time() - start_time
    print()
    print("Время работы: {} (индекс: {}, поиск дублей: {})".format(
        format_time(total_time),
        format_time(idx_time),
        format_time(search_time)))


if __name__ == "__main__":
    main()
