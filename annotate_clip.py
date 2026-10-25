"""
annotate_clip.py — mark when cheating actually happened in a recording

THE GAP THIS CLOSES: a clip's label ("innocent" / "suspicious") says what
the clip is about, not WHEN anything happened in it. Without the when,
the only thing that can be measured is whether the whole clip got
flagged, and a paper needs to know how many of the actual cheating
moments were caught and how many false alarms were raised in between
(see evaluation.py). This tool is how a person writes the when down.

It plays the clip and you mark stretches of time with the keyboard. The
result is a small JSON file in <clips folder>/annotations/<clip>.json,
which experiment.py reads. The previous version is kept as .bak every
time you save.

WHAT TO MARK: every stretch in which the participant was doing the
cheating behaviour, from the moment it started (head or eyes begin to
move away) to the moment it stopped (back on their own paper). Mark what
you SEE, never what the engine said: the whole point is an independent
answer to compare the engine with. Engine output is deliberately not
shown here.

    python annotate_clip.py clips/suspicious_side_glance.mp4
    python annotate_clip.py clips/x.mp4 --from-csv marks.csv   import times written during the session

KEYS
    space          play / pause
    left / right   back / forward 2 s        (also A / D)
    , / .          one frame back / forward (while paused)
    - / =          slower / faster playback
    1 - 8          choose the behaviour type for the next mark
    tab            next student (S1-S4), for multi-person clips
    I              mark the START of a stretch at the current time
    O              mark the END: the stretch is added
    X              delete the stretch under the playhead
    Z              undo the last added stretch
    C              toggle "every cheating moment is marked" (complete)
    S              save        Q / Esc   save and quit
    click on the timeline bar to jump there
"""

import argparse
import os
import sys
import time

import evaluation as ev

STUDENTS = ["S1", "S2", "S3", "S4"]
# BGR colours per behaviour, in BEHAVIOUR_KEYS order.
COLOURS = [(47, 67, 184), (45, 122, 208), (39, 162, 201), (120, 70, 150),
           (60, 130, 60), (140, 90, 40), (90, 90, 200), (110, 110, 110)]
KEY_LEFT, KEY_RIGHT = 2424832, 2555904
BAR_H = 86


class Annotator:
    """All the state and every key action, with no window in it, so the
    logic can be tested without a screen."""

    def __init__(self, ann, duration, fps):
        self.ann = ann
        self.duration = duration
        self.fps = fps or 25.0
        self.type_i = 0
        self.student_i = 0
        self.pending = None          # start time of a stretch being marked
        self.history = []            # added intervals, for undo
        self.dirty = False
        self.message = ""

    @property
    def type_key(self):
        return ev.BEHAVIOUR_KEYS[self.type_i]

    @property
    def student(self):
        return STUDENTS[self.student_i]

    def mark_in(self, t):
        self.pending = t
        self.message = f"start marked at {ev.fmt_time(t)} - press O at the end"

    def mark_out(self, t):
        if self.pending is None:
            self.message = "press I at the start first"
            return None
        a, b = sorted((self.pending, t))
        self.pending = None
        if b - a < 0.2:
            self.message = "too short (under 0.2 s) - not added"
            return None
        iv = {"start": round(a, 2), "end": round(b, 2), "type": self.type_key,
              "student": self.student, "note": ""}
        self.ann["intervals"].append(iv)
        self.history.append(iv)
        self.dirty = True
        self.message = f"added {self.type_key} {ev.fmt_time(a)}-{ev.fmt_time(b)} for {self.student}"
        return iv

    def delete_at(self, t):
        for iv in self.ann["intervals"]:
            if iv["start"] <= t <= iv["end"] and iv["student"] == self.student:
                self.ann["intervals"].remove(iv)
                if iv in self.history:
                    self.history.remove(iv)
                self.dirty = True
                self.message = f"deleted {iv['type']} {ev.fmt_time(iv['start'])}-{ev.fmt_time(iv['end'])}"
                return iv
        self.message = "no stretch under the playhead for " + self.student
        return None

    def undo(self):
        if not self.history:
            self.message = "nothing to undo"
            return None
        iv = self.history.pop()
        if iv in self.ann["intervals"]:
            self.ann["intervals"].remove(iv)
        self.dirty = True
        self.message = "undone"
        return iv

    def toggle_complete(self):
        self.ann["complete"] = not self.ann.get("complete", False)
        self.dirty = True
        self.message = ("marked COMPLETE: every cheating moment is marked" if self.ann["complete"]
                        else "marked incomplete")

    def set_type(self, i):
        if 0 <= i < len(ev.BEHAVIOUR_KEYS):
            self.type_i = i
            self.message = f"type: {self.type_key} - {ev.BEHAVIOURS[self.type_key]}"

    def next_student(self):
        self.student_i = (self.student_i + 1) % len(STUDENTS)
        self.message = "marking for " + self.student

    def render(self, frame, t, playing, speed):
        """Frame with the overlay and the timeline bar under it."""
        import cv2
        import numpy as np
        h, w = frame.shape[:2]
        out = np.full((h + BAR_H, w, 3), 245, np.uint8)
        out[:h] = frame
        # top band
        cv2.rectangle(out, (0, 0), (w, 26), (25, 25, 25), -1)
        status = (f"{ev.fmt_time(t)} / {ev.fmt_time(self.duration)}   "
                  f"{'PLAY' if playing else 'PAUSE'} x{speed:g}   "
                  f"[{self.type_i + 1}] {self.type_key}   {self.student}   "
                  f"{'COMPLETE' if self.ann.get('complete') else 'incomplete'}"
                  f"{'  *' if self.dirty else ''}")
        cv2.putText(out, status, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        # timeline
        y0 = h + 12
        cv2.rectangle(out, (10, y0), (w - 10, y0 + 22), (215, 215, 215), -1)
        X = lambda s: int(10 + (w - 20) * min(max(s / max(self.duration, 0.01), 0), 1))
        for iv in self.ann["intervals"]:
            col = COLOURS[ev.BEHAVIOUR_KEYS.index(iv["type"]) % len(COLOURS)]
            row = STUDENTS.index(iv["student"]) if iv["student"] in STUDENTS else 0
            ya = y0 + row * 5
            cv2.rectangle(out, (X(iv["start"]), ya), (max(X(iv["end"]), X(iv["start"]) + 2), ya + 22 - row * 5),
                          col, -1)
        if self.pending is not None:
            cv2.rectangle(out, (X(min(self.pending, t)), y0 + 16), (X(max(self.pending, t)), y0 + 22),
                          (0, 0, 220), -1)
        cv2.line(out, (X(t), y0 - 4), (X(t), y0 + 26), (0, 0, 0), 2)
        help_ = "space play  <- -> seek  1-8 type  I start  O end  X delete  Z undo  C complete  S save  Q quit"
        cv2.putText(out, help_, (10, y0 + 42), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (60, 60, 60), 1, cv2.LINE_AA)
        if self.message:
            cv2.putText(out, self.message, (10, y0 + 62), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (30, 30, 160), 1,
                        cv2.LINE_AA)
        return out

    def time_at_x(self, x, width):
        return max(0.0, min(self.duration, (x - 10) / max(width - 20, 1) * self.duration))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mark when cheating happened in a recorded clip.")
    ap.add_argument("clip", help="path to the clip, e.g. clips/suspicious_side_glance.mp4")
    ap.add_argument("--annotator", default="", help="your name or initials, stored in the file")
    ap.add_argument("--participant", default="", help="participant id, e.g. P07")
    ap.add_argument("--from-csv", help="import start,end,type[,student,note] rows, then open")
    ap.add_argument("--no-mirror", action="store_true", help="show the clip as recorded "
                                                             "(default mirrors it, as the engine does)")
    a = ap.parse_args(argv)

    import cv2
    if not os.path.exists(a.clip):
        raise SystemExit(f"no such clip: {a.clip}")
    folder = os.path.dirname(os.path.abspath(a.clip))
    clip = os.path.basename(a.clip)
    path = ev.annotation_path(folder, clip)
    if os.path.exists(path):
        ann = ev.load_annotation(folder, clip)
        print(f"editing {path} ({len(ann['intervals'])} stretches)")
    else:
        ann = ev.new_annotation(clip, a.annotator)
    if a.annotator:
        ann["annotator"] = a.annotator
    if a.participant:
        ann["participant"] = a.participant
    if a.from_csv:
        added = ev.intervals_from_csv(a.from_csv)
        ann["intervals"].extend(added)
        print(f"imported {len(added)} stretches from {a.from_csv}")

    cap = cv2.VideoCapture(a.clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    tool = Annotator(ann, n / fps if n else 0.0, fps)
    if a.from_csv:
        tool.dirty = True

    win = "annotate - " + clip
    cv2.namedWindow(win)
    state = {"seek": None, "w": 640}

    def on_mouse(event, x, y, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN and y > state.get("h", 10 ** 6):
            state["seek"] = tool.time_at_x(x, state["w"])

    cv2.setMouseCallback(win, on_mouse)

    def save():
        p = ev.save_annotation(folder, tool.ann)
        tool.dirty = False
        tool.message = f"saved {os.path.relpath(p)}"
        print(tool.message)
        if not tool.ann.get("complete"):
            print("  note: marked incomplete - it counts toward catches, not toward false alarms. "
                  "Press C once every cheating moment is marked.")

    playing, speed = False, 1.0
    frame, t = None, 0.0

    def seek(sec):
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, sec) * 1000.0)

    ok, frame = cap.read()
    if not ok:
        raise SystemExit("could not read the clip")
    t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
    last = time.time()
    while True:
        if state["seek"] is not None:
            seek(state["seek"]); state["seek"] = None
            ok, f2 = cap.read()
            if ok:
                frame = f2
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if playing and time.time() - last >= 1.0 / (fps * speed):
            last = time.time()
            ok, f2 = cap.read()
            if ok:
                frame = f2
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            else:
                playing = False
                tool.message = "end of clip"
        shown = frame if a.no_mirror else cv2.flip(frame, 1)
        img = tool.render(shown, t, playing, speed)
        state["w"], state["h"] = img.shape[1], shown.shape[0]
        cv2.imshow(win, img)
        k = cv2.waitKeyEx(10)
        if k == -1:
            if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                break
            continue
        ch = chr(k).lower() if 0 <= k < 256 else ""
        if ch == " ":
            playing = not playing
        elif k == KEY_LEFT or ch == "a":
            state["seek"] = t - 2.0
        elif k == KEY_RIGHT or ch == "d":
            state["seek"] = t + 2.0
        elif ch == "," and not playing:
            state["seek"] = t - 1.0 / fps
        elif ch == "." and not playing:
            ok, f2 = cap.read()
            if ok:
                frame, t = f2, cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        elif ch == "-":
            speed = max(0.25, speed / 2)
        elif ch == "=" or ch == "+":
            speed = min(4.0, speed * 2)
        elif ch in "12345678" and ch:
            tool.set_type(int(ch) - 1)
        elif k == 9:
            tool.next_student()
        elif ch == "i":
            tool.mark_in(t)
        elif ch == "o":
            tool.mark_out(t)
        elif ch == "x":
            tool.delete_at(t)
        elif ch == "z":
            tool.undo()
        elif ch == "c":
            tool.toggle_complete()
        elif ch == "s":
            save()
        elif ch == "q" or k == 27:
            break
    if tool.dirty:
        save()
    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
