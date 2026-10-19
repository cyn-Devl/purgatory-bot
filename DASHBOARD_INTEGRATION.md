# Gilbert dashboard integration — deployment checklist

This branch is a **draft**, not automatically deployed. Test before merging.

## Bot host environment variables
Set these in Bot-Hosting.net (not GitHub source code):
- `DISCORD_TOKEN` — existing token
- `SUPABASE_URL` — same Supabase project URL as the dashboard
- `SUPABASE_SERVICE_ROLE_KEY` — private server-side key; never publish it

The bot fetches `public.guild_modules` settings via Supabase REST. Changes are cached for up to 20 seconds. If credentials are missing or Supabase is temporarily unreachable, the reader returns empty/default settings. Supabase's service-role key must stay exclusively on trusted backend services.

## Discord configuration
Enable **Server Members Intent** and **Message Content Intent** in the Discord developer portal. Grant Manage Roles (and place Gilbert's role above assigned roles), Manage Messages for automod, and Send Messages/Read Message History/Add Reactions as needed.

## Features in this draft
- Custom `!command` responses configured per guild in dashboard
- Welcome and goodbye messages
- Human and bot autoroles
- Purgatory role, immunity role IDs, Deepest Pits duration and log channel
- Basic automod for invite URLs, links and excessive mentions (antiSpam is not implemented)
- Reaction role add/remove by configured message ID, emoji and role ID
- Global slash-command sync for servers beyond the original test guild

## Known limitations and rollout warnings
- The dashboard Purgatory toggle defaults to **off**; enabling this branch without enabling Purgatory in the dashboard can disable Purgatory commands. Confirm settings first.
- **Do not delete or reset `gilbert.db`**. It contains saved Purgatory roles used by `/unpurg` and timed releases. Confirm the bot host persists this file between restarts.
- Legacy role-name moderator permissions still apply; `moderatorRoleIds` and `staffRoleIds` are not yet wired.
- Moderation commands, anti-spam, full event logging, premium access control and dashboard AI settings are not yet implemented.
- AI still uses the legacy general channel and may be active if `GEMINI_API_KEY` is set. This branch does not enforce premium subscription entitlements.
- Global slash commands can take time to propagate in Discord; existing guild-specific commands may need a separate cleanup later.
- No automated integration tests or live Discord tests have been run.

## Suggested test plan
1. In a disposable Discord test server, save welcome, autoroles, custom commands and reaction roles in the dashboard.
2. Verify settings are isolated by server and take effect after 20 seconds.
3. Confirm welcome and goodbye events, bot vs human autoroles and role hierarchy.
4. Verify reaction add/remove, invite/link/mention filtering and moderators' exemptions.
5. Verify Purgatory stores roles, restores them on `/unpurg`, and auto-releases after restart with persistent SQLite storage.
6. Verify commands appear in a second installed server.
7. Confirm logs show no Supabase auth or permission errors.
