# Bifocal Buffer v0.1 — Design Contract

**Status:** DRAFT for owner review. No code until ratified.
**Depends on:** STR v0.3 (proposal/approval/receipt semantics, hash-chained ledger)
**One-line:** The terminal buffer as a shared memory bus — one glyph plane, read
natively by both human and LLM, where every human edit is an STR proposal.

---

## 1. Problem

Every agent UI pays a double-serialization tax: agent state -> JSON ->
render layer -> pixels; then, for the agent to "see" what the human sees,
vision models or accessibility dumps. The human's view and the agent's
ground truth are different objects, kept in sync by translation.

Bifocal Buffer removes the translation. One object — the character grid —
is simultaneously:

1. **Human UI**: a clean terminal dashboard (borders, gauges, status lines).
2. **Machine state**: a deterministic, parseable spatial AST the LLM reads
   as raw text with zero token overhead beyond the buffer itself.

The buffer is not a display of state. The buffer IS the state.

## 2. Core Model

### 2.1 The Glyph Plane

A fixed-size grid of cells. Each cell is a 4-tuple:

```
Cell = (row: int, col: int, glyph: char, attr: Attr)
Attr = { fg: 3-byte RGB | INHERIT, bg: 3-byte RGB | INHERIT,
         flags: {bold, dim, reverse} }
```

`glyph` is ONE Unicode codepoint (no zero-width, no homoglyphs, no
combining sequences in v0.1 — see §8 Non-goals). Braille and half-blocks
are legal payloads; they are treated as ordinary single glyphs.

### 2.2 Canonical Cell Serialization

For hashing and parsing, every cell serializes deterministically:

```
cell_repr = f"{row},{col}:{codepoint_hex}:{fg_hex}|{bg_hex}|{flags_bitmask}"
```

- `INHERIT` attrs serialize as `--`.
- `flags_bitmask` is a 3-bit integer: bold=4, dim=2, reverse=1.
- The canonical frame serialization is cells in row-major order, joined by
  `;`, EXCLUDING trailing whitespace-only cells at the end of each row
  (right-trim per row, but row order preserved). This keeps empty space
  cheap without ambiguity: a row of all-space serializes as its row index
  followed by nothing.

```
frame_repr = W;H;cursor_row,cursor_col;cell(0,0);cell(0,1);...;cell(H-1,W-1)
```

Width, height, and **cursor position are part of the frame**. Where the
human points is part of the state (§6).

### 2.3 Frame Hash

```
frame_hash = sha256(frame_repr)
```

Every render is content-addressed. "What is on screen" becomes a
64-character identity that can be quoted, diffed, chained, and signed.

## 3. Region Protocol v0.1

The grammar is spatial, not textual. Deterministic parsing requires a
small locked vocabulary — like early HTML: few tags, strict rules.

### 3.1 Region Types (exactly six in v0.1)

| Type | Border glyph set | Must contain | Semantics |
|------|------------------|--------------|-----------|
| `DASH` | `╭─╮│╰╯` | `title :: ` line in top border | dashboard region; key-value body |
| `PROPOSAL` | `┏━┓┃┗┛` | `prop_` id, `action:`, `expected_version:` | a pending STR proposal |
| `RECEIPT` | `┌─┐│└┘` | `ledger_seq:`, `receipt_hash:` | an executed receipt |
| `STATUS` | `▐▌` (left edge only) | one row per agent/task | fleet heartbeat list |
| `LOG` | `░▒` (top edge only) | append-only lines | rolling event stream |
| `VOID` | (none) | all-space cells | unallocated plane |

Rules:
- Borders are **required** for typed regions and **illegal** elsewhere.
  A box-drawing glyph appearing outside a region boundary is a parse
  error. The grammar cannot be spoofed by decorative art.
- Region headers are keys: the first line inside a region is
  `key :: value` pairs, whitespace-aligned. Body cells are values.
- Regions do not overlap and do not share border cells (no ambiguity).
- `VOID` is the default; anything outside a typed region must parse as
  VOID, else the frame is invalid.

### 3.2 Parse Result (the spatial AST)

```
Frame = {
  frame_hash: str,
  regions: [ {
    type, origin: (r,c), size: (h,w),
    title: str?, attrs: {key: value},   // parsed header line
    body_cells: [cell_repr...],         // canonical, untrimmed
    body_text: str                      // LLM-friendly reading form
  } ],
  cursor: (row, col),
  cursor_region: region_ref | null
}
```

`body_text` is the plain reading form an LLM consumes: region type +
title + key-values + body, in raster order. Deterministic, so the same
frame always yields byte-identical text for the model.

### 3.3 Dual-Semantics Invariants

I1. **Round-trip:** parse(serialize(frame)) == frame. No lossy cells.
I2. **Canonical hash:** two frames that render identically to a human and
    are identical to the machine must hash equally — no hidden state.
I3. **No heuristic parsing:** every glyph is either grammar or VOID.
    Anything else is an invalid frame, never a "best guess".

## 4. Edit-as-Proposal (the STR mapping)

Human edits are not applied to the plane directly. They are **STR
proposals over the frame**:

```
{
  proposal_id: "prop_<hash10>",
  action: "CELL_WRITE",
  base_version: <frame_hash of current plane>,
  diff: { set: { cells: [ (row,col,glyph,attr)... ],
                 cursor: (row,col) } },
  request_id: <keystroke uid>,
  client_signature: HMAC(secret, canonical(diff + base_version))
}
```

The runtime applies the same fail-closed semantics it already enforces:

- **Stale frame -> ERR_STALE_VERSION.** You edited a frame that has since
  changed (agent updated telemetry, another approval landed). Rejected.
- **Exactly-once.** A keystroke sent twice executes once; replay returns
  the existing receipt.
- **Scope binding.** The approval capability token binds to
  (proposal_id, expected frame_hash, region_origin, action). A prompt
  can authorize "write one glyph into cell (12,40)" and nothing else.
- **Receipt.** Every applied edit appends a hash-chained receipt.
  The frame ledger and the state ledger are the same ledger.

The plane's version counter IS the state version. There is one ledger.

## 5. Cursor as Signing Surface

Because cursor position is in `frame_repr` (§2.2), an authorization can
be scoped to *where the human was pointing*:

```
approval_scope = (frame_hash, cursor_region, cursor_cell)
```

"Cursor parked on the APPROVE field of proposal prop_89f2a + one
keystroke" is a structurally complete, replay-proof authorization event.
Focus is part of the signature. A logged-in terminal session cannot
authorize what is not on screen under the cursor.

## 6. Derived Chrome (anti-spoof rendering)

Status decorations — border pattern, density ramp, status glyph — are
render outputs of:

```
chrome = derive(sha256(frame_hash + proposal_sig + prev_receipt_hash))
```

A forged approval prompt cannot reproduce the derived chrome without the
sovereign key; the geometry shears visibly. Visual integrity is the
cryptographic validation. No steganography, no invisible characters —
the visible layout is the signature.

## 7. v0.2 Acceptance Gates (what code must prove)

G1. **Round-trip:** 1,000 randomized valid frames -> serialize ->
    hash -> parse -> serialize: byte-identical, hash-stable.
G2. **Grammar strictness:** mutated border glyph outside a region ->
    invalid frame (fail closed, never heuristic).
G3. **Edit-as-proposal:** terminal edit on a stale frame ->
    ERR_STALE_VERSION; same keystroke replayed -> same receipt, zero
    side effects; scoped edit outside its permitted cells -> rejected.
G4. **Cursor binding:** approval with cursor outside the proposal region
    -> rejected.
G5. **Ledger unity:** N edits on the plane produce N receipts with an
    unbroken chain; /verify passes; tamper a cell in the DB -> chain
    breaks at that receipt.
G6. **LLM legibility:** given only `body_text` of a PROPOSAL region, a
    model can answer "what action, at what version, is being asked?" —
    measured by deterministic fixtures, not vibes.

## 8. Non-Goals (v0.1)

- No zero-width/homoglyph steganography (fragile across fonts/terms).
- No Sixel/Kitty graphics — the glyph plane is portable ASCII+Unicode.
- No cellular automaton substrate yet (Wireworld scheduling is a
  separate future contract; the frame grammar must not block it).
- No 60fps shader loop — bifocal is about state semantics, not fidelity.
  Density ramps and Braille are legal content, not requirements.

## 9. Open Questions (owner decision)

Q1. Region size limits per type? (a 10,000-line LOG region is a plane
    discipline problem, not a parser problem)
Q2. Is `cursor` in the hash (§2.2) or in the proposal only (§4)?
    Strawman: in both — frame hash includes it, proposals restate it.
Q3. Should STATUS/LOG regions be non-ledgered (read-only projections of
    external state) to avoid receipt spam for telemetry refreshes?
    Strawman: yes — only DASH/PROPOSAL/RECEIPT regions are ledgered state.
Q4. Plane dimensions: fixed at startup (terminal resize = new plane
    version) vs. live-resize?

---

*Ratified v0.1 locks §2-§6 as-is; §9 answers amend before v0.2 code.*
