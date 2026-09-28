"""
session_report.py — SessionReportBuilder

Produces a professional, printable PDF summary of a proctoring session.

WHY THIS EXISTS
───────────────
Everything the system observed has, until now, lived in three places
none of which survive the session in a usable form: numbers on a live
dashboard that vanish when it closes, JSONL logs only a developer can
read, and JPEGs in a folder with no context attached.

If a student is going to be asked about their conduct, somebody needs a
document. Not a log dump — a document: what was observed, when, for how
long, what it is and is not consistent with, with the evidence images
attached and the method explained. That is the difference between a
monitoring tool and an accountability tool, and it is the artefact an
institution would actually need.

WHAT IT CONTAINS
────────────────
  1. Executive summary and a risk table covering every student
  2. Cross-student findings (correlation.py), with their caveats
  3. One section per student: score history graph, recognised
     behavioural patterns, full event timeline, evidence images, a
     plain-English narrative and a recommended action
  4. Seat-occupancy history, so any identity change during the session
     is visible rather than hidden
  5. A methodology note explaining, in plain English, how the numbers
     were reached

DESIGN CONSTRAINTS
──────────────────
  * Built with ReportLab's Platypus flow layout so content paginates
    naturally rather than overflowing fixed positions.
  * Plain ASCII punctuation throughout the PDF text. ReportLab's
    built-in Helvetica lacks some glyphs, and a missing glyph renders
    as a solid black box in the finished document.
  * Every image is loaded defensively — a missing or corrupt evidence
    file must degrade to a placeholder line, never abort the report.
    A report that fails to generate is worth nothing.
  * Generation happens off the capture thread (see the caller), because
    building a PDF takes far longer than one video frame.
"""

import os
import time

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String

from config import Config
from narrative import SessionNarrator, _dur, _name

# Muted, print-friendly palette. Saturated screen colours look wrong on
# paper and waste ink on a document meant to be printed.
_INK = colors.HexColor("#1a1d23")
_MUTED = colors.HexColor("#5c6470")
_RULE = colors.HexColor("#c8cdd6")
_BG_ALT = colors.HexColor("#f4f6f9")
_LEVEL_COLORS = {
    "FLAG": colors.HexColor("#b3261e"),
    "ALERT": colors.HexColor("#c77700"),
    "WATCH": colors.HexColor("#8a6d00"),
    "OK": colors.HexColor("#2e7d32"),
    "CALIBRATING": colors.HexColor("#5c6470"),
    "No assessment": _MUTED,
}


def _sty():
    ss = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle(
            "t", parent=ss["Title"], fontName="Helvetica-Bold",
            fontSize=19, leading=23, textColor=_INK, spaceAfter=2),
        "subtitle": ParagraphStyle(
            "st", parent=ss["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13, textColor=_MUTED, spaceAfter=10),
        "h1": ParagraphStyle(
            "h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
            fontSize=13.5, leading=17, textColor=_INK,
            spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle(
            "h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
            fontSize=11, leading=14, textColor=_INK,
            spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle(
            "b", parent=ss["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=14, textColor=_INK,
            alignment=TA_LEFT, spaceAfter=6),
        "small": ParagraphStyle(
            "sm", parent=ss["Normal"], fontName="Helvetica",
            fontSize=8, leading=11, textColor=_MUTED, spaceAfter=4),
        "caveat": ParagraphStyle(
            "cv", parent=ss["Normal"], fontName="Helvetica-Oblique",
            fontSize=8.5, leading=12, textColor=_MUTED,
            leftIndent=8, spaceAfter=6),
    }
    return s


def _ascii(text) -> str:
    """
    ReportLab's built-in Helvetica has no glyph for several characters
    this codebase uses freely in comments and descriptions (em dash,
    curly quotes, arrows). A missing glyph does not raise — it silently
    renders as a solid black box, which looks like corruption in a
    printed document. Substituting is safer than hoping.
    """
    if text is None:
        return ""
    repl = {
        "\u2014": "-", "\u2013": "-", "\u2019": "'", "\u2018": "'",
        "\u201c": '"', "\u201d": '"', "\u2192": "->", "\u2194": "<->",
        "\u00b7": "-", "\u2026": "...", "\u00b1": "+/-", "\u2265": ">=",
        "\u2264": "<=",
    }
    out = str(text)
    for a, b in repl.items():
        out = out.replace(a, b)
    return out.encode("ascii", "replace").decode("ascii")


def _P(text, style):
    return Paragraph(_ascii(text), style)


class SessionReportBuilder:
    """
    Builds the PDF. Construct with the session's data, call build().

    Reads only; never mutates any pipeline state.
    """

    def __init__(self, students, verdicts, patterns_by_slot, correlations,
                 event_logger=None, archive=None, evidence_dir=None,
                 score_logs=None, session_id="session", session_start=None,
                 roster=None):
        self.students = students
        self.verdicts = verdicts or {}
        self.patterns = patterns_by_slot or {}
        self.correlations = correlations or []
        self.event_logger = event_logger
        self.archive = archive
        self.evidence_dir = evidence_dir
        self.score_logs = score_logs or {}
        self.session_id = session_id
        self.session_start = session_start
        self.roster = roster
        # Apply registered names immediately at construction rather than
        # during build(), so any analysis the caller runs against these
        # students afterwards already refers to people rather than seats.
        if roster is not None:
            for _sid, _st in (students or {}).items():
                _st.display_name = roster.display_name(_sid, _st.label)
        self.narrator = SessionNarrator()
        self.s = _sty()

    # ── public ─────────────────────────────────────────────────────

    def build(self, out_path: str, t: float = None) -> str:
        t = t or time.time()
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        start = self.session_start or min(
            (s.temporal._session_start for s in self.students.values()),
            default=t,
        )

        doc = BaseDocTemplate(
            out_path, pagesize=A4,
            leftMargin=18 * mm, rightMargin=18 * mm,
            topMargin=16 * mm, bottomMargin=18 * mm,
            title=f"Proctoring Session Report - {self.session_id}",
            author="CPG-127 Head Pose Intelligence Engine",
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin,
                      doc.width, doc.height, id="main")
        doc.addPageTemplates([
            PageTemplate(id="all", frames=[frame], onPage=self._decorate)
        ])

        story = []
        story += self._cover(t, start)
        story += self._roster_section()
        story += self._risk_table(t)
        story += self._correlations()
        for sid in sorted(self.students.keys()):
            story += self._student_section(sid, t)
        story += self._occupancy()
        story += self._methodology()

        doc.build(story)
        return out_path

    # ── page furniture ─────────────────────────────────────────────

    def _decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_MUTED)
        canvas.drawString(
            18 * mm, 10 * mm,
            _ascii(f"CPG-127 Proctoring Report  |  {self.session_id}"))
        canvas.drawRightString(
            A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(_RULE)
        canvas.setLineWidth(0.4)
        canvas.line(18 * mm, 13 * mm, A4[0] - 18 * mm, 13 * mm)
        canvas.restoreState()

    # ── sections ───────────────────────────────────────────────────

    def _cover(self, t, start):
        gen = time.strftime("%d %B %Y, %H:%M:%S", time.localtime(t))
        began = time.strftime("%d %B %Y, %H:%M:%S", time.localtime(start))
        out = [
            _P("Examination Proctoring Report", self.s["title"]),
            _P(f"Session {self.session_id}  |  generated {gen}",
               self.s["subtitle"]),
            _P("Executive summary", self.s["h1"]),
            _P(self.narrator.session_overview(
                self.students, list(self.students.keys()), t, start,
                self.verdicts, self.patterns), self.s["body"]),
            _P(f"Session began {began} and ran for {_dur(t - start)}.",
               self.s["small"]),
        ]
        return out

    def _roster_section(self):
        """
        Who was in the room, as declared by the invigilator. Placed
        before the findings because a reader needs to know who the
        report is about before reading what it says about them.
        """
        if self.roster is None or self.roster.registered_count() == 0:
            return []
        rows = [["Seat", "Name", "Roll number", "Registered at"]]
        for rec in self.roster.all_students():
            rows.append([
                f"S{rec['slot'] + 1}", rec.get("name") or "-",
                rec.get("roll_no") or "-", rec.get("registered_at_str") or "-",
            ])
        tbl = Table(rows, colWidths=[16 * mm, 62 * mm, 40 * mm, 32 * mm],
                    repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _BG_ALT),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        note = ("Registered by the invigilator before the exam."
                if self.roster.is_locked else
                "Registered by the invigilator. This roster was not locked "
                "during the session, so seats could still change.")
        return [_P("Students registered", self.s["h1"]), tbl,
                _P(note, self.s["small"])]

    def _risk_table(self, t):
        rows = [["Student", "Assessment", "Peak", "Events", "Away time",
                 "Recommended action"]]
        styles = [
            ("BACKGROUND", (0, 0), (-1, 0), _BG_ALT),
            ("TEXTCOLOR", (0, 0), (-1, 0), _INK),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i, sid in enumerate(sorted(self.students.keys()), start=1):
            st = self.students[sid]
            v = self.verdicts.get(sid, "CALIBRATING")
            action, _ = self.narrator.recommendation(
                st, self.patterns.get(sid) or [], v)
            rows.append([
                _name(st), v,
                f"{st.fs.max_probability:.0f}%" if st.calib.is_calibrated else "-",
                str(st.temporal.total_count()),
                f"{st.temporal.total_away_s:.1f}s",
                action,
            ])
            styles.append(("TEXTCOLOR", (1, i), (1, i),
                           _LEVEL_COLORS.get(v, _INK)))
            styles.append(("FONTNAME", (1, i), (1, i), "Helvetica-Bold"))
            if i % 2 == 0:
                styles.append(("BACKGROUND", (0, i), (-1, i),
                               colors.HexColor("#fbfcfd")))
        tbl = Table(rows, colWidths=[20 * mm, 26 * mm, 15 * mm, 16 * mm,
                                      22 * mm, 75 * mm], repeatRows=1)
        tbl.setStyle(TableStyle(styles))
        return [_P("Summary of all students", self.s["h1"]), tbl,
                _P("Assessment reflects observed head-orientation behaviour only. "
                   "No recommendation stronger than 'refer for review' is ever "
                   "produced by this system; every decision rests with a person.",
                   self.s["small"])]

    def _correlations(self):
        out = [_P("Cross-student analysis", self.s["h1"]),
               _P(self.narrator.correlation_summary(self.correlations),
                  self.s["body"])]
        for c in self.correlations:
            out.append(_P(
                f"<b>{_ascii(c['pair'][0])} and {_ascii(c['pair'][1])} - "
                f"{_ascii(c['label'])}</b> "
                f"({int(c['confidence'] * 100)}% confidence)", self.s["h2"]))
            out.append(_P(c["description"], self.s["body"]))
            if c.get("caveat"):
                out.append(_P(c["caveat"], self.s["caveat"]))
        return out

    def _student_section(self, sid, t):
        st = self.students[sid]
        pats = self.patterns.get(sid) or []
        v = self.verdicts.get(sid, "CALIBRATING")
        action, reasoning = self.narrator.recommendation(st, pats, v)

        out = [PageBreak(),
               _P(f"{_name(st)} - detailed assessment", self.s["title"]),
               _P(f"Assessment: {v}  |  peak score "
                  f"{st.fs.max_probability:.0f}%  |  "
                  f"{st.temporal.total_count()} events recorded",
                  self.s["subtitle"])]

        out.append(_P("What was observed", self.s["h1"]))
        out.append(_P(self.narrator.student_summary(st, pats, t, v),
                      self.s["body"]))

        graph = self._score_graph(sid)
        if graph:
            out.append(_P("Suspicion score over the session", self.s["h2"]))
            out.append(graph)

        if pats:
            out.append(_P("Behavioural patterns recognised", self.s["h1"]))
            for p in pats:
                tag = {"suspicious": "Concern", "exonerating": "Mitigating",
                       "neutral": "Neutral"}.get(p["severity"], "Note")
                out.append(_P(
                    f"<b>{_ascii(p['label'])}</b> - {tag}, "
                    f"{int(p['confidence'] * 100)}% confidence", self.s["h2"]))
                out.append(_P(p["description"], self.s["body"]))

        tl = self._timeline(st)
        if tl:
            out.append(_P("Event timeline", self.s["h1"]))
            out.append(tl)

        ev = self._evidence(st)
        if ev:
            out.append(_P("Captured evidence", self.s["h1"]))
            out += ev

        out.append(_P("Recommended action", self.s["h1"]))
        out.append(_P(f"<b>{_ascii(action)}</b>", self.s["body"]))
        out.append(_P(reasoning, self.s["body"]))
        return out

    def _score_graph(self, sid):
        """
        Score history as a simple line chart. Drawn with primitives
        rather than a charting package so the report has no extra
        dependency and cannot fail on a plotting backend.
        """
        pts = list(self.score_logs.get(sid) or [])
        if len(pts) < 3:
            return None
        w, h = 160 * mm, 42 * mm
        d = Drawing(w, h)
        d.add(Rect(0, 0, w, h, fillColor=colors.white,
                   strokeColor=_RULE, strokeWidth=0.5))

        t0, t1 = pts[0][0], pts[-1][0]
        span = max(t1 - t0, 1.0)
        pad_l, pad_b, pad_t = 12 * mm, 7 * mm, 4 * mm
        pw, ph = w - pad_l - 5 * mm, h - pad_b - pad_t

        # Threshold guides give the line meaning without a legend.
        # Labelled on the RIGHT: on the left they sat at the same
        # heights as the 0/50/100 axis labels and printed on top of
        # them, which rendered as unreadable overlapping text.
        for val, lbl, col in (
            (Config.LEVEL_HIGH, "HIGH", colors.HexColor("#e5b4b0")),
            (Config.LEVEL_MODERATE, "MOD", colors.HexColor("#eed9b0")),
        ):
            y = pad_b + (val / 100.0) * ph
            d.add(Line(pad_l, y, pad_l + pw, y, strokeColor=col,
                       strokeWidth=0.6, strokeDashArray=[2, 2]))
            d.add(String(pad_l + pw + 1.5, y - 2, lbl,
                         fontSize=5.5, fillColor=_MUTED))

        for val in (0, 50, 100):
            y = pad_b + (val / 100.0) * ph
            d.add(String(pad_l - 9 * mm, y - 2, f"{val}%",
                         fontSize=6, fillColor=_MUTED))

        coords = []
        for (ts, sc) in pts:
            x = pad_l + ((ts - t0) / span) * pw
            y = pad_b + (max(0.0, min(sc, 100.0)) / 100.0) * ph
            coords += [x, y]
        d.add(PolyLine(coords, strokeColor=colors.HexColor("#1f5f9e"),
                       strokeWidth=1.0))
        d.add(Line(pad_l, pad_b, pad_l + pw, pad_b,
                   strokeColor=_RULE, strokeWidth=0.5))
        d.add(String(pad_l, 2, _ascii(time.strftime("%H:%M:%S",
                     time.localtime(t0))), fontSize=6, fillColor=_MUTED))
        d.add(String(pad_l + pw - 18 * mm, 2, _ascii(time.strftime(
            "%H:%M:%S", time.localtime(t1))), fontSize=6, fillColor=_MUTED))
        return d

    def _timeline(self, st):
        # Sort by time. The list is NOT reliably chronological: when a
        # returning student's archived record is merged back in, older
        # events are appended after newer ones. An out-of-order timeline
        # in a formal document is a credibility problem, not a cosmetic
        # one — a reader checking the times would conclude the whole
        # report was unreliable.
        events = sorted(st.temporal.events, key=lambda e: e.timestamp)
        if not events:
            return None
        shown = events[-Config.REPORT_MAX_TIMELINE_ROWS:]
        rows = [["Time", "Direction", "Duration", "Deviation"]]
        for e in shown:
            rows.append([
                time.strftime("%H:%M:%S", time.localtime(e.timestamp)),
                e.direction, f"{e.duration:.2f}s", f"{e.z_score:.1f} sd",
            ])
        tbl = Table(rows, colWidths=[26 * mm, 26 * mm, 24 * mm, 26 * mm],
                    repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _BG_ALT),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        out = tbl
        if len(events) > len(shown):
            return KeepTogether([
                out,
                _P(f"Showing the most recent {len(shown)} of {len(events)} "
                   f"events. The complete record is in the session log.",
                   self.s["small"]),
            ])
        return out

    def _evidence(self, st):
        """
        Embed saved evidence images. Every load is guarded: a missing or
        unreadable file becomes a note, never an exception. A report that
        refuses to generate because one JPEG is corrupt is useless.
        """
        # Only the evidence FOLDER is needed to find evidence. Requiring
        # an archive as well meant that any session where no seat ever
        # changed hands - i.e. the normal case - silently produced a
        # report with no evidence images at all.
        if not self.evidence_dir:
            return []
        recs = []
        try:
            latest = getattr(self, "_evidence_index", None)
            if latest is None:
                latest = self._index_evidence()
                self._evidence_index = latest
            # Keyed by the SEAT LABEL, not the display name: evidence
            # JSON files record student as "S1" etc, written before any
            # registration may have happened. Looking them up by the
            # registered name silently returned nothing, so every report
            # for a named student lost all its evidence images.
            recs = latest.get(st.label, [])
        except Exception:
            recs = []
        if not recs:
            return []

        out = []
        for rec in recs[: Config.REPORT_MAX_EVIDENCE_IMAGES]:
            path = os.path.join(self.evidence_dir, rec.get("image", ""))
            cap = (f"{rec.get('time', '?')}  |  score "
                   f"{rec.get('score', '?')}%  |  {rec.get('reason', '')}")
            if os.path.isfile(path):
                try:
                    out.append(Image(path, width=95 * mm, height=63 * mm,
                                     kind="proportional"))
                    out.append(_P(cap, self.s["small"]))
                    continue
                except Exception:
                    pass
            out.append(_P(f"[evidence image unavailable] {cap}",
                          self.s["small"]))
        return out

    def _index_evidence(self):
        """Scan the evidence folder once and group records by student."""
        import json
        idx = {}
        if not self.evidence_dir or not os.path.isdir(self.evidence_dir):
            return idx
        for root, _dirs, files in os.walk(self.evidence_dir):
            for fn in sorted(files):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, fn)) as f:
                        rec = json.load(f)
                except Exception:
                    continue
                # The evidence folder also holds occupancy_records.json,
                # which is a LIST, not an evidence record. Calling .get()
                # on it raised and silently wiped out the entire evidence
                # section of the report — the failure was invisible
                # because the caller swallowed it. Check the shape.
                if not isinstance(rec, dict):
                    continue
                lbl = rec.get("student")
                if lbl:
                    idx.setdefault(lbl, []).append(rec)
        return idx

    def _occupancy(self):
        if not self.archive:
            return []
        recs = self.archive.all_records()
        if not recs:
            return []
        out = [PageBreak(), _P("Seat occupancy history", self.s["h1"]),
               _P("Where a seat changed hands during the session, the previous "
                  "occupant's record is preserved rather than discarded, so no "
                  "part of the session is missing from this report.",
                  self.s["body"])]
        rows = [["Seat", "Occupant", "From", "To", "Peak", "Events", "Outcome"]]
        for r in recs:
            outcome = ("reunited with returning student"
                       if r.get("merged_into_slot") is not None
                       else "seat taken by a different person")
            rows.append([
                r.get("label", "?"), f"#{r.get('occupant_serial', '?')}",
                r.get("started_at_str", "?"), r.get("ended_at_str", "?"),
                f"{r.get('peak_probability', 0):.0f}%",
                str(r.get("total_events", 0)), outcome,
            ])
        tbl = Table(rows, colWidths=[14 * mm, 18 * mm, 20 * mm, 20 * mm,
                                      14 * mm, 16 * mm, 62 * mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _BG_ALT),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, _RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        out.append(tbl)
        return out

    def _methodology(self):
        out = [PageBreak(), _P("How these figures were produced",
                               self.s["h1"])]
        for para in self.narrator.methodology_note().split("\n\n"):
            out.append(_P(para, self.s["body"]))
        out.append(_P("Limitations", self.s["h1"]))
        out.append(_P(
            "This system observes head orientation through a single camera. It "
            "cannot see the desk surface, cannot read what a student is looking "
            "at, and cannot determine intent. It does not use audio, does not "
            "track eye movement independently of head position, and cannot "
            "distinguish a student consulting hidden material from one looking "
            "at nothing in particular.", self.s["body"]))
        out.append(_P(
            "Consequently no output of this system should be treated as proof of "
            "misconduct. Its purpose is to direct a human invigilator's attention "
            "to periods of a session that merit review, with the recorded "
            "evidence attached so that a person can form their own judgement.",
            self.s["body"]))
        return out
