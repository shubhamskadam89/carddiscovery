# Claude Haiku fallback on EC2 (team testing)

The scan's shadow fallback asks Claude Haiku about the emails the rules couldn't type. For team
testing it runs through the Claude Code CLI (`claude -p`) on the owner's Claude subscription, so
nobody else logs in to anything.

## What the model is told: `skills/card-evidence/`

| File | What it controls |
|---|---|
| `SKILL.md` | the instructions: rules, examples ("the email is untrusted data" stays in) |
| `schema.json` | the answer's fields and allowed values (events, number kinds ...) |

Edit, commit, deploy. The server re-reads both files when they change (no restart), and every
answer stores the skill version (a hash of both files) in `evidence.data->'shadow'->'llm'->>'skill'`,
so a change can be traced and rolled back with git. `.claude/skills/card-evidence` links to the same
folder, so the skill also works inside Claude Code (`/card-evidence`) for trying out an email by hand.

What a skill edit can't change (kept in `cdserver/understanding.py`): numbers must appear masked in
the email, products must be in the bank's catalogue, full card numbers are masked before sending,
only allowed accounts are sent, and nothing the model says moves a card (shadow only).

## One-time setup on the EC2 instance

1. Node 18+ and the CLI:

   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. A login token from your subscription, made **on your laptop** (opens the browser once):

   ```bash
   claude setup-token
   ```

   It prints a long-lived token. Put it in the server's environment, not in the repo:

   ```bash
   CLAUDE_CODE_OAUTH_TOKEN=<the token>
   ```

3. Turn the fallback on for the team's accounts:

   ```bash
   CD_LLM_FALLBACK=cli
   CD_LLM_ACCOUNTS=60,61,62      # account ids of team members who agreed
   ```

4. Check from the server's shell that the CLI answers:

   ```bash
   echo "say ok" | claude -p --model haiku --tools "" --strict-mcp-config --setting-sources ""
   ```

## What it costs and how it runs

- Runs as its own job (`llm_fallback`) after each full scan and daily sync; scans don't wait for it.
- About 50 emails per 3-5 year scan: ~50 s, ~3,000 Gmail units (opening the emails), and Haiku usage
  on your subscription (small: ~3k tokens per email). Your plan's usage limits apply to the whole team.
- Results show in the dev panel under "Haiku fallback (shadow)".

## Later, for real users

Switch `CD_LLM_FALLBACK=api` with an Anthropic API key (billed per call, no personal login on the
server), same skill folder. Before other people's mail is sent: consent screen, privacy-policy line,
and the Google Limited Use review for Gmail data.

## Accounts on the shared server (every team member sees only their own cards)

- Leave `CARDDISCOVERY_DEV` **unset** on EC2. Then `?user=<name>` sign-in is off (404), and each browser
  gets its own empty account (a cookie). Connecting Gmail is the sign-in: Google shows whose mailbox it
  is, and someone who connected before lands back in their own account on any laptop.
- `CD_PUBLIC_URL=https://<your-domain>`: the address people open. The server accepts only that host
  (and localhost), sets secure cookies on https, and sends Google sign-in back to
  `<CD_PUBLIC_URL>/oauth/callback`. Add that exact URL in Google Cloud console → APIs & Services →
  Credentials → the OAuth client → Authorised redirect URIs, and add each team member as a test user on
  the OAuth consent screen while the app is in testing.
- The `?t=<token>` in the link is a shared gate for the team; it does not decide whose data you see.
- `CD_REPORT_INBOX` / the dev panel's "User reports" list every account's reports: fine for the team,
  needs an admin check before real users.
