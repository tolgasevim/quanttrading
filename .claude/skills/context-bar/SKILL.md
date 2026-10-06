---
name: context-bar
description: Show how much of the session's token budget is used, as a text bar. Use when the user types /context-bar or asks how full the context is.
disable-model-invocation: true
---

# Context bar

Print one text bar that shows how much of the token budget is used. Do not use any tool.

## Steps

1. Find the **budget at the start**: the first `total_tokens` value you saw in this session
   (the "tokens left" figure). If you cannot find it, say so and stop.
2. Find the **tokens left now**: the newest `total_tokens` value.
3. Calculate: `used = start - left` and `percent = used / start * 100`, rounded to a whole number.
4. Draw a bar of 20 cells. Each full cell is 5%. Use `█` for used and `░` for free.
5. Print exactly this, with real numbers:

   ```
   Context  [██░░░░░░░░░░░░░░░░░░] 10%  used 1.5M of 15M  left 13.5M
   ```

   Write large numbers with `K` (thousands) or `M` (millions) and one decimal at most.

## Rules

- This is an estimate of the session budget. Say "about" in a short note under the bar.
- When the context was summarised, the budget figure still counts the whole session. Do not
  reset it.
- If the bar is 80% or more, add one line: "Context is nearly full. Finish the current step and
  start a new session soon."
- Use short sentences and simple words (ASD-STE100 style), as the owner asked.
