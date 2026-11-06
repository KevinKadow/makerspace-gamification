# The Ledger bot icon

The Ledger is a sentient leatherbound grimoire: worn chestnut leather, substantial brass corners, embossed brows, narrow amber eyes, and a restrained mouth. Its watchful expression suits the system AI personality. Slightly cartoonish proportions keep it legible without making it cute.

| Asset | Dimensions | Use |
| --- | --- | --- |
| [ledger-bot-512.png](ledger-bot-512.png) | 512 × 512 | Slack app/bot icon upload and desktop rendering |
| [ledger-bot-36.png](ledger-bot-36.png) | 36 × 36 | Mobile-size export and readability reference |
| [ledger-bot-master.png](ledger-bot-master.png) | 1,254 × 1,254 | Original generated source for future exports |

All three files are square PNGs with an opaque charcoal background. The two delivery sizes are high-quality bicubic exports of the same source, with no cropping or changes to the illustration. Their actual dimensions and appearance were checked, including the mobile export at native size. The icon has no lettering or pre-rounded corners; Slack applies its own corner treatment.

Upload **ledger-bot-512.png** through The Ledger's Slack app settings under **Basic Information → Display Information → App icon**. Slack adapts this single uploaded icon to its display sizes; the 36-pixel file is supplied for previews and other small-icon uses, not a second Slack upload. Slack accepts app icons from 512 × 512 through 2,000 × 2,000 pixels. See [Slack app design](https://docs.slack.dev/concepts/app-design/) and [icon size requirements](https://docs.slack.dev/reference/methods/apps.icon.set).

The manifest's app-profile background is `#1C1C1C`, matching the icon's charcoal surround. The PNG itself is an installation asset; it is not embedded in the manifest or uploaded by the bot at runtime. See [Slack installation](../docs/SLACK.md).

## Generation record

Created on October 2, 2026 using the built-in image generation tool. The complete final prompt is preserved in [ledger-bot-prompt.txt](ledger-bot-prompt.txt). This was a new illustration without reference-image inputs. The source was preserved unchanged; the exact-size exports were produced with Windows System.Drawing high-quality bicubic resampling.

The pre-existing `ledger_newbie.png` through `ledger_adept.png` files are rank artwork and remain separate from the bot identity.
