# GramGPT product inventory (observed UI)

Observed on 2026-09-24 at `https://gramgpt.io/panel` in an authenticated browser session. This is a functional map of the visible product, not a copy of GramGPT source code, prompts, or backend behavior.

## Scope and confidence

- The panel rendered for this account. Module pages showed a preview banner: settings can be viewed, but execution requires a subscription or module purchase.
- The account manager contained accounts, but module account pickers showed no available accounts. The account import dialog was visible, but import controls were disabled by the account-slot limit. No jobs, purchases, uploads, messages, or configuration submissions were made.
- A few pages expose real account and conversation data. Those values are intentionally excluded from this inventory.
- This inventory records visible controls and stated behavior only. Hidden server logic, paid-only states, and results from actual runs remain unverified.

## Product navigation

### Main sections

- Account manager (`/panel`): status counters, account table, import/add account, proxy pool, filters, column picker, photo library, trash, per-row info/actions.
- Tasks (`/panel/tasks`): active jobs and history, filters for all/modules/accounts/parsers; active runs are described as server-side and cancellable.
- My statistics (`/panel/my-statistics`): dashboard, accounts, history; period filters and export. History categories include comments, reactions, messages, story views, NeuroShilling, direct-message campaigns, group campaigns, story mentions, and invitations.
- Support (`/panel/support`): ticket list, status filter, new ticket, Telegram contact.
- User profile (`/panel/user/profile`): personal settings, security, notifications, partner program, API, subscription and purchase history, connected accounts. Sensitive profile values are omitted.
- Channel database (`/panel/channel-base`): saved channels/groups, folders, language/type/size/source/activity filters, import/add by link, live/weak/dead/no-data health filters.
- Parsing history (`/panel/parsing-history`): module/type filters, search, status and date range.
- Checkers (`/panel/checkers`): link/channel checks and phone-to-Telegram checks, using selected checker accounts.

### Shared module workflow

Most task modules reuse the same screen structure:

1. Choose accounts using search, country and role filters; optionally require working proxies, hide busy accounts, or switch to lightweight grouped rendering.
2. Add targets from text, saved history, folders, account subscriptions, or the internal database, depending on the module.
3. Configure work mode, content, timing, limits, and module-specific filters.
4. Review a configuration-readiness panel, then launch or schedule the job. A log/progress section and a results/history link are provided.
5. Save reusable launch presets in modules that expose the preset control.

Common additions include JSON schema import before account selection, a launch-preset panel, an optional advanced-settings panel, delayed start, and a cancel-stuck-jobs control. Some modules also allow multiple saved launch templates to be selected together.

Account-manager filters include status, geo, spam-block state, proxy status, work status, stories, username, bio link, personal channel, folder link, and role. Optional columns include account age, added date, geo, registration date, last check, stories, and work status. The import dialog distinguishes TData and `.session`, accepts an optional source/seller label, and displays slot availability.

## Module inventory

### NeuroCommenting (`/panel/modules/neuro-commenting`)

- Automated AI comments on posts from watched Telegram channels.
- Comment modes: random, keyword-based, or all posts; probability control; count-based or time-based work mode; minimum post length; new/existing-post ordering.
- Keyword mode has a dedicated keyword field and reports missing keywords in launch readiness. Time mode uses a duration slider; zero is described as unlimited.
- Targets can be entered by username/link, selected from prior jobs, or chosen by folder; an account-subscriptions switch is available. The folder state exposes a folder field and add/select control.
- Message setup supports AI prompts and saved prompts, sticker comments, and image-plus-caption comments.
- Additional controls include posting as a channel, comment-visibility checking, gradual pacing for new accounts, automatic/manual language mode, work hours, auto-responder mode, delay presets, FloodWait handling, and quarantine threshold.
- Advanced settings include concurrency/duplicate handling and per-account pacing controls. They were not tuned or run.
- Includes launch diagnostics, logs, comment statistics/history, and a channel blocklist.

### NeuroChatting (`/panel/modules/neuro-chatting`)

- AI replies in Telegram groups, with interval or trigger-based reaction mode and count/time work mode.
- Group targets can be entered by username/link, selected from previous jobs, or chosen by folder. “Only joined chats” is an option.
- AI prompt settings include saved/system prompts, a reply-condition text area, language mode, work hours, and conversation-context depth.
- Also exposes auto-responder modes, an organic-promotion toggle, delay settings, presets, advanced settings, message history, and an account blocklist.

### Mass Reactions (`/panel/modules/mass-react`)

- Reactions on group messages and channel-post comments; target sources include account subscriptions, direct links, previous jobs, and folders.
- Emoji selection, random/sequential emoji mode, monitoring/existing-message mode, duration, total and per-account caps, and reaction probability.
- Optional rules limit reacting when a post already has many reactions or limit the operation to the first N comments.
- Has launch diagnostics, reaction history, and a channel blocklist.

### Mass Looking (`/panel/modules/mass-looking`)

- Story views for channel/user targets; an option can build targets from group participants and select users who have posted in a chat.
- Controls cover delay between views, FloodWait handling, total/per-account view caps, avoiding repeat views for a configured period, heart likes, and other story reactions.
- Provides preview of selected accounts/targets, launch diagnostics, and story-view history.

### Direct-message campaigns (`/panel/modules/spam-pm`)

- Target sources: a manual list, account contacts, or existing account dialogs; accepts file import and has a clear-all control.
- A recipient-preview panel accompanies a configurable message chain with text/template or AI-personalized first-message modes.
- Direct and trigger-based delivery modes are shown. The screen also exposes post-reply choices, including no reply or NeuroDialogs.
- Supports multistep chains, media/sticker content, message variants, timing/limits, a launch preview, logs, campaign history, and a blocklist.

### Group-message campaigns (`/panel/modules/spam-groups`)

- Targets can be entered as usernames/links/IDs or Telegram folder links; can also use chats already associated with accounts, delivery/refusal history, or the internal database.
- Message chain supports a shared text/template or AI-generated first message; chat and sender variables are described.
- Offers skip-on-send-error behavior, repeat-round mode, and post-campaign direct-message auto-responder choices.
- Includes timing/limit controls, logs, campaign history, and a group blocklist.

### Story mentions (`/panel/modules/story-mentions`)

- Select target people manually, from account contacts, or from account dialogs; supports file upload.
- A media pool accepts story photos/videos; separate caption and link-sticker fields are provided.
- Controls include mentions per story, daily story limit per account, time/count volume modes, “prime only,” skip previously mentioned targets, and story lifetime.
- Includes run diagnostics, statistics, and mention history.

### NeuroShilling (`/panel/modules/neuro-shilling`)

- Landing screen offers a campaign-creation wizard: choose accounts, describe a scenario, launch; says later runs can be repeated in one click.
- The campaign-creation form was not opened because the button may create persistent campaign state.

### Account warming (`/panel/modules/warming`)

- Manual and automatic modes; scheduled activity window, timezone, random breaks, stage-based adaptation, and session duration/start spread.
- Action categories displayed include reactions, reading channels, inter-account dialogs, story views, and group joins; target groups/channels can be selected or entered.
- Per-action details and presets are present. This inventory does not recommend imitation or evasion behavior.

### NeuroDialogs (`/panel/modules/neuro-dialogs`)

- Inbox for dialogs across selected accounts; search, unread/waiting-for-reply filters, sound toggle, refresh, full-screen view, and new-dialog control.
- Separate switch for AI auto-replies; page copy says monitoring can run without sending replies.
- Contact exports are offered as CSV and text, including a subset waiting for a reply for over a day. Do not copy any conversation data from this account into the project.

### GGR account check (`/panel/modules/ggr`)

- Account-quality score page with account selection, status filters, sorting, per-check balance/cost display, and a geographic benchmark.
- “Check all” and file-based imports are visible. They were not used because checking may incur a charge.

### Checkers (`/panel/checkers`)

- Two modes: validate Telegram links/channels or check whether phone numbers are registered on Telegram.
- Uses selected checker accounts; accepts target lists; includes readiness checks and delayed start.

### Parsing users (`/panel/modules/parsing-users`)

- Collect participants from public/open groups or use a supplied username list.
- Visible filters include participant limit, bots/deleted/scam/active state, username/photo/Premium, admins, story activity, and optional profile classification. Result controls include compact/grid views, sorting, copy links/IDs, and export.

### Parsing messages (`/panel/modules/parsing-messages`)

- Search users in chat messages using optional keywords and a date window; limit analyzed messages and choose whether replies/forwards are included.
- Reuses account/profile/activity filters and has result sorting, copy, export, logs, and parsing history.

### Parsing comments (`/panel/modules/parsing-comments`)

- Collect users from comments on channel posts using optional keywords, post/comment caps, and minimum comment length.
- Reuses bot/deleted/scam/profile/activity filters; an optional switch stores comment text. Results can be sorted, copied, or exported.

### Search parser (`/panel/modules/parsing-search?type=channels|groups`)

- Search Telegram channels/groups by keyword tags or a generated prompt; supports keyword batches, endings, language, subscriber range, rating, text/language/activity/comment filters, result cap, and request delays.
- Prompt mode asks for a topic description (up to 500 characters), a request count, and query language, then generates search queries. The screen also has “similar channels” and quick-start templates.
- Results have compact/grid views, sorting, copy, and export; existing parsing history can be excluded.

### Database parser (`/panel/modules/parsing-database`)

- Search GramGPT's internal channel/group database without Telegram accounts.
- Filters include type, category, language, comments, access type, subscriber range, post activity, comments per post, text terms, excluded terms, and whether results are already in the user's base.
- Can save results into a folder and shows an estimated charge before search. No search was run.

## Design implications for a CoreBot implementation

- Use one shared account/target picker and one shared background-job lifecycle across modules. Keep module-specific configuration schemas and validations separate.
- Persist a versioned configuration snapshot with each job so the UI can reproduce prior runs and presets can be reused safely.
- Separate target sources from resolved targets; show count, invalid rows, duplicates, and skipped items before any job can start.
- Put run readiness and estimated impact/cost beside the launch control; retain logs, structured outcomes, cancellation, and history after navigation or browser close.
- Reuse CoreBot's existing account, parser, outbound-queue, task, and history services where possible instead of introducing parallel Telegram clients or database engines.
- For a compliant product scope, constrain messaging/commenting to owned communities or recipients who explicitly opted in; keep AI-generated outbound content reviewable, provide stop/suppression lists, and avoid stealth, platform-limit evasion, or personal-data harvesting.

## Still to inspect

- Detailed option states for account-specific settings, prompt editor, auto-responder setup, parser templates, and result export menu.
- Account information/action menus, proxy pool, photo library, trash, add-account form, and non-preview account roles/projects.
- NeuroShilling campaign wizard and paid/active module states, which are not accessible in the current preview without persistent setup or purchase.
- Screenshots of each long page at multiple scroll positions. The current browser tool exposes the rendered accessibility tree and viewport captures; it does not yet provide a saved screenshot archive.
