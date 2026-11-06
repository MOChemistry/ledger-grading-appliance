# The assignment formatter

> Experimental software for evaluation by school and district IT professionals; see [DISCLAIMER.md](../DISCLAIMER.md).

Added in v1.1.0. The **FORMAT ASSIGNMENT** window takes an existing assignment — a PDF, or pasted questions — and re-typesets it into the Ledger format described in [ASSIGNMENT_FORMAT.md](ASSIGNMENT_FORMAT.md), so the result can be scored by the machine. The scoring path itself is unchanged.

## How a format run works

1. The operator opens FORMAT ASSIGNMENT, names the assignment, chooses the document type and model, and attaches the source.
2. The source assignment — page images, extracted text, and on the Claude route the PDF itself — is sent through the local AI service to the configured provider, together with the formatting instructions in [`formatter/`](../formatter/).
3. **The model replies with a short Python build script, and the machine runs that AI-written code locally** with the project's own Python interpreter, sandboxed with **bubblewrap**: no network access, a 120-second limit, and write access only to a scratch folder under the operator's workspace. If bubblewrap is unavailable the run proceeds unsandboxed and the result says so.
4. The finished PDF is checked against the same layout rules the scoring engine relies on (tags, boxes, fonts, page marks). The operator sees the result page by page and can ask for revisions in plain language; each revision is another model call.
5. Results, scripts, and the conversation are stored under `Ledger Workspace/06 Formatted Assignments/`.

## Cost and models

Each format run makes one or more calls to the provider **on the operator's own API key**, and long documents are expensive to format; all charges appear on the account whose key is entered. Claude (`claude-fable-5`) is the tested model. Gemini models can be selected, but results vary and are not part of the tested configuration.

## What to evaluate

- The sandbox: confirm bubblewrap is installed and that a format run reports it as active.
- The output: the formatter is experimental; a qualified person should review every formatted assignment before it is printed or scored.
- The data path: the source assignment leaves the machine (see the table in the [README](../README.md)); review that against your institution's policies before formatting any real material.
