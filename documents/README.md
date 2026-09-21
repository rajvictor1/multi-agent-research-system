# Put your own documents here — **.txt only**

This folder is what the **document analysis agent** reads. Anything you drop in becomes
private evidence the web search agent could never find.

## Why .txt and not PDF

A PDF has to be extracted before an agent can read it, and that extraction is usually the
most expensive part of a whole run — a 200-page report can burn more tokens than every web
search combined. Paste the two or three sections you actually need into a `.txt` file and
the cost is close to zero.

So: open the PDF, copy the relevant pages, save as `.txt`, drop it here.

## Two ways to add files

1. drop `.txt` files into this folder (subfolders work too — `documents/ev/`,
   `documents/clinical/`), or
2. use the **upload box** in the Streamlit app's Run tab — it accepts `.txt` and saves here.

## The three sample files

These ship with the project so you can demo without uploading anything. **All three are
fictional** — invented publishers, invented figures, clearly labelled at the top of each
file. They are teaching props, not sources.

| File | Use with | What it demonstrates |
|---|---|---|
| `sample_ev_battery_analyst_extract.txt` | `query_ev_batteries.txt` | a paid report saying **$71.4B** for 2024 where public trackers say ~$91B — and the scope note explaining why (pack-level, excludes installation and China domestic) |
| `sample_cgm_pivotal_trial.txt` | `query_clinical_evidence.txt` | **n=512**, 78% responder rate, peer reviewed |
| `sample_cgm_pilot_study.txt` | `query_clinical_evidence.txt` | **n=48**, 41% responder rate, preprint, no control arm |

The last two disagree on purpose. Watch the synthesis agent put both in the **Contested
findings** section with their sample sizes, instead of averaging 78% and 41% into 60%.
The pilot study even says in its own text that it should not be pooled with randomized
data — that is the kind of methodology note a document carries and a web snippet does not.

Delete the samples when you want your own material to be the only evidence.

## Rules the agent follows

- Every claim it extracts carries the file name and page or section, e.g.
  `sample_cgm_pivotal_trial.txt p.9`. Otherwise the figure could not be found again.
- Numbers are copied exactly, with units. It never paraphrases a figure.
- It uses Grep and Glob to find the right passage before reading, so a long file does not
  have to be read end to end.
- **If this folder has no .txt files, the document agent is not spawned at all.** Spawning
  an agent to search an empty folder costs money and returns an error.

Your own files are git-ignored — only this README and the `sample_*.txt` files are tracked,
so a paid report never ends up in a repo.
