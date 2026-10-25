# CPG-127 — Recording protocol for the participant study

This is the plan for collecting the data the paper needs. Follow it the
same way for every participant: a study is only as good as its
consistency.

**What this study measures.** For each cheating behaviour, how many of the
actual cheating moments the system catches (event recall), how many false
looks and false alarms it raises on honest behaviour (per hour), and how
it compares with a simple fixed-angle rule and with itself with each part
switched off. All of that is computed by `experiment.py` once the clips
are recorded and annotated.

---

## 0. Before recording anyone

1. **Ask sir about ethics approval.** Recording people's faces for research
   normally needs the institute's approval. Do not start without it.
2. **File the provisional patent first** if sir wants one. A paper, a
   preprint or a public dataset can count as public disclosure.
3. **Consent.** Every participant signs a short form that says:
   - what is recorded (video of their face and upper body, no audio)
   - why (to test an exam-monitoring research system)
   - that the cheating is *acted*, on instruction, and says nothing about them
   - where the video is kept (this laptop only), and that it is deleted
     after the study unless they separately agree to its release
   - that they can withdraw at any time and their video will be deleted
   - a separate yes/no box: "my video may be released in an anonymised
     research dataset". Only clips with a yes here can ever be shared.
4. **Participant IDs only.** P01, P02, … Names live on the consent forms
   and nowhere else, never in file names or notes.

**Target: 20 participants (at least 15).** With about 12 cheating moments
each, 20 people give about 240 marked moments, which narrows the catch-rate
interval to roughly ±5 points. Try to include people with and without
glasses, and different heights.

---

## 1. Setup (keep it identical for everyone)

| | |
|---|---|
| Camera | at or slightly **below** face height, angled slightly up, 60–90 cm away, centred on the participant |
| Why | a camera above the face sees the crown of the head when they write; that is the single biggest cause of lost readings found so far |
| Light | in front of the participant or to the side, never a bright window behind them |
| Background | plain if possible; nobody walking behind |
| Desk | a sheet of paper and a pen; a phone placed face down to one side; notes on a second sheet to one side |
| Seat | the participant stays seated the whole session |

Write the camera height, lighting and glasses in the session log
(`research/session_log.csv`).

---

## 2. The solo session (about 9 minutes)

The operator (one of you) stands **behind the camera**, holds this script
and a phone stopwatch, and says each instruction quietly. Never say
"cheat": say what to do ("eyes to the left, three times"). Do a 1-minute
practice first so the participant knows every action, then record.

Half the participants get **Order A**, half **Order B** (the same blocks,
cheating blocks in reverse). That stops the results depending on what
came first, when people are fresh or tired.

| # | ~time | Block | Type (for marking) |
|---|---|---|---|
| 0 | 0:00–0:30 | Sit and write normally. **No instructions.** The system learns their normal posture here. | — |
| 1 | 0:45 | Write normally | honest |
| 2 | 0:40 | Turn head to the neighbour's side for ~3 s, back to paper. **3 times.** | head_turn |
| 3 | 0:30 | Think: look up, chin on hand, look back down | honest |
| 4 | 0:40 | Keep head still, move **only the eyes** to one side for 2–3 s. **3 times.** | eyes_only |
| 5 | 0:30 | Stretch, rub neck, shift in the chair | honest |
| 6 | 0:40 | Glance down into the lap for ~3 s. **3 times.** | look_down |
| 7 | 0:20 | Drink water, adjust glasses | honest |
| 8 | 0:20 | **One** long look at the notes to the side, 6–8 s | long_look |
| 9 | 0:45 | Write normally | honest |
| 10 | 0:40 | Pick up the phone, use it in the lap ~10 s, put it back. **2 times.** | phone |
| 11 | 0:15 | **One** natural glance at the clock or door | honest |
| 12 | 0:40 | Head turn, **other side**. 3 times. | head_turn |
| 13 | 0:40 | Eyes only, **other side**. 3 times. | eyes_only |
| 14 | 0:30 | Write normally | honest |

**Order B:** blocks 0 and 1 as above, then the cheating blocks in the order
13, 12, 10, 8, 6, 4, 2, with the honest blocks 3, 5, 7, 9, 11, 14 between
them in their usual order.

Block 11 matters: one honest glance away is exactly what a fair system
must NOT treat as cheating.

Record with:
```
python record_clip.py P01_A --folder study --label suspicious --seconds 560 --note "order A, glasses, cam 5cm below eyes"
```
(`--label suspicious` because the session contains cheating; the
annotation says exactly when.)

---

## 3. Group sessions (2–4 people side by side)

This is the part of the project nobody else has, and the part with no
real evidence yet. Seat 2–4 participants in a row, all in view of one
camera at the same distance. Record about 6 minutes:

| # | Block | Type |
|---|---|---|
| 0 | Everyone writes normally for 30 s (calibration) | — |
| 1 | Write normally, 45 s | honest |
| 2 | **Mutual glance**: two neighbours look toward each other at the same time, ~2 s. 4 times. | copy_neighbour (mark both, each with the other as partner) |
| 3 | **One-way copying**: one person looks at the neighbour's paper ~2 s after the neighbour writes something. 4 times. | copy_neighbour (mark only the copier) |
| 4 | **Shared room event**: the operator drops a book or says "ten minutes left". Everyone looks up. | honest — must NOT look like cheating |
| 5 | **Signal**: one person shows fingers or passes a note under the desk. 2 times. | signal |
| 6 | Write normally, 45 s | honest |

```
python record_clip.py G01 --folder study --label suspicious --seconds 380 --note "G01: P01 P02 P03, left to right"
```

---

## 4. Marking what happened (annotation)

Right after each session, while you still remember it:

```
python annotate_clip.py study/P01_A.mp4 --participant P01 --annotator YM
```

Rules, the same for everyone:
- **Mark every individual look**, not the whole block: three glances are
  three stretches.
- **Start** = the first frame the head or eyes begin to move away.
  **End** = back on their own paper.
- For the phone, mark the whole time it is in use.
- **Do not mark honest blocks.** Unmarked time is honest time.
- Mark what you **see**, never what the dashboard said.
- When every cheating moment in the clip is marked, press **C** (complete),
  then **S**. An incomplete clip still counts toward catches, but not
  toward false alarms.
- Group clips: press **Tab** to choose the student (S1–S4, the same labels
  the engine draws on the video). For copying, write the other person in
  the note.

Times written on paper during the session can be imported with
`--from-csv` (columns `start,end,type,student,note`; times in seconds
or m:ss).

**A second marker.** Have a second person annotate the clips of 4 or 5
participants independently. Reviewers ask how reliable the marking is, and
two people agreeing is the answer. The comparison script is not built yet.

---

## 5. Running the experiment

```
python video_batch.py study          # replay every clip through the engine
python experiment.py --curve         # baselines, ablations, trade-off curve
```

Results go to `research/results/<date_time>/`. Read `table.md` first.

The first line of every result says whether the replay reproduced the
engine exactly. If it says **UNTRUSTED**, stop and find out why before
using any number.

---

## 6. Data handling

- Videos stay on this laptop, in `study/`. Never upload them anywhere.
- `study/` must never be committed to git.
- Delete a participant's clips the moment they withdraw.
- Publish numbers, not faces, unless that participant ticked the dataset box.
