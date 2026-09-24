# ZEOS demos

Applications built on [ZEOS](https://github.com/metacognitionai/zeos), a transformer
operating system: a deterministic kernel that schedules, protects and pages LLM jobs.

Each demonstration is its own directory and its own project, with its own README saying
how to run it. What they share is the shape: a *case* of small text files declares the
behaviours, their priorities, the pipes between them and who may ask for what, and the
ZEOS kernel runs it. The Python around the case is the driver: the adapters that turn a
keystroke or a model's answer into a pipe write, and a pipe write into something a person
can see.

Here are some example demos:
<table>
  <tr>
    <td width="45%">
      <video src="https://github.com/user-attachments/assets/0d50df71-6f42-45bb-9fb7-7442af57b1ea" autoplay loop muted playsinline></video>
    </td>
    <td width="45%">
      <video src="https://github.com/user-attachments/assets/d0dd53ca-b76f-4897-a70b-acbc09eb38b5" autoplay loop muted playsinline></video>
    </td>
  </tr>
</table>

## Example of running a demo

```bash
cd zeos-chat
uv sync --extra claude
cp .env.example .env        # then put your Claude key in it
uv run zeos-chat serve --open
```

Each README says what else it needs.
Every demonstration contains a canned demo that can run without a model API key as well.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) says where a change belongs and what must hold before
a pull request. [AGENTS.md](AGENTS.md) has the conventions for code and prose, which
agree with the ones in `zeos` where the two overlap.

## Licence

This repository is MIT, see [LICENSE](LICENSE). ZEOS itself is AGPL-3.0-only and is
imported as a dependency, never vendored.
