# Examples

Use either track-list format as a starting point:

```bash
groove-serpent analyze "Side A.flac" --tracklist examples/tracklist.json
```

Durations may be written as seconds, `M:SS`, or `H:MM:SS`. Titles and durations are optional in the application, but an expected track count or track list makes automatic boundary choice much more reliable.

Generate a synthetic local demonstration file from the repository root:

```bash
python scripts/create_demo_audio.py --output-dir demo
```

The script prints the analyze and review commands to run next.
