# Classification Rules

Reference for classifying AI talks candidates. Read this once before starting Phase 2.

## How labels work

`label` is the search bucket that surfaced the candidate — NOT a confirmed speaker or topic. Treat it as a weak hint only.

- `Sam Altman` = came from the Sam Altman watchlist query, not that Sam Altman is definitely the speaker.
- `Topic: OpenAI Talks` = came from a topic search, not that the video is necessarily about OpenAI.
- `Channel: Dwarkesh Patel` = came from that channel watchlist, not that the guest is Dwarkesh Patel.

If `label` conflicts with the title or description, trust the title/description. Do not accept a candidate just because its `label` looks relevant.

## Criteria by label type

**Person watchlist** (`label` = a person's name):
- The labeled person must be a direct guest or speaker in this specific video.
- Do NOT expand this rule to colleagues, employees, associates, or anyone else affiliated with the labeled person. Only the exact person in the label counts. For example, if the label is "Sam Altman", an interview with a researcher at Sam Altman's startup is still a reject — Sam Altman himself must be the guest.
- Reject if the person is only being discussed, quoted, or mentioned in passing.
- Reject if the person appears only in channel promo boilerplate (e.g. "This show has recently featured Sam Altman" in a description for an interview with someone else).
- Reject reactions, summaries, news reports *about* the person.

**Channel watchlist** (`label` = "Channel: X"):
- Does it feature an AI researcher, founder, or thought leader speaking in their own words?
- These come from curated high-signal channels, so the bar is: genuine AI talk or interview.

**Topic search** (`label` = "Topic: X"):
Both checks must pass — reject if either fails:
1. Is this a genuine first-person talk or interview (not a third-party explainer/commentary)?
2. If the candidate has an `org` field: can you confirm from the title or description that the speaker is affiliated with **that specific org** (not any AI org — only the one in the `org` field)? For example, if `org` is "Meta AI", only accept if the speaker is confirmed to be from Meta AI. An OpenAI researcher appearing in a "Meta AI Talks" search should be rejected under this label. If the description is absent or too vague to confirm affiliation, reject.

## Deduplication

**Across candidates:** Multiple candidates may be different channels uploading the same event. Accept only one. Prefer: original/official channel > named news outlet > generic reupload.

**Against state.json:** If a candidate matches an event already in `state.json items` (same person, same event, similar timeframe), reject it even though the video ID is new.

## Buckets

Each candidate goes into one of three buckets:

- **Accept**: genuine first-person talk or interview where the speaker/guest is clearly identified from the title or description.
  - *Person watchlist*: the labeled person is confirmed as the direct guest or speaker.
  - *Topic search*: the talk is a genuine first-person talk AND the speaker is confirmed to be affiliated with the `org` in the candidate (see "Criteria by label type" above).
  - *Channel watchlist*: the video is a genuine AI talk or interview from the curated channel.
  - *Re-uploads*: when multiple candidates cover the same event, prefer the original/official channel. However, if no original-channel version exists among the candidates, accept the best available re-upload (longest duration, most complete).
- **Reject**: clearly derivative, duplicate, or confirmed irrelevant. Enough information to be certain.
- **Uncertain**: not enough information (empty description, ambiguous title). Leave out of both accepted and rejected — it will resurface next run.