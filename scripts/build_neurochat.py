from pathlib import Path
import re

chunk = Path("bot/handlers/_neuro_chunk.txt").read_text(encoding="utf-8")
rep = [
    ("cb_mailing_mod_neuro", "cb_neurochat_open"),
    ("cb_mailing_neuro_sampling_menu", "cb_neurochat_sampling_menu"),
    ("cb_mailing_neuro_sampling_reset", "cb_neurochat_sampling_reset"),
    ("cb_mailing_neuro_sampling_param", "cb_neurochat_sampling_param"),
    ("cb_mailing_neuro_toggle", "cb_neurochat_toggle"),
    ("cb_mailing_neuro_model", "cb_neurochat_model"),
    ("cb_cancel_mailing_neuro", "cb_cancel_neurochat_fsm"),
    ("cancel_mailing_neuro", "cancel_neurochat_fsm"),
    ("cb_mailing_neuro_prompt", "cb_neurochat_prompt"),
    ("cb_mailing_neuro_link", "cb_neurochat_link"),
    ("cb_mailing_neuro_stoplist", "cb_neurochat_stoplist"),
    ("cb_mailing_neuro_unstop", "cb_neurochat_unstop"),
    ("MailingNeuroFSM", "NeuroChatFSM"),
    ("mailing_mod_neuro_", "neurochat_open_"),
]
for a, b in rep:
    chunk = chunk.replace(a, b)
chunk = chunk.replace(
    "Отправьте одно число (целое или с десятичной точкой).\n\n"
    "❌ Отмена: <code>/cancel</code>",
    "Отправьте одно число (целое или с десятичной точкой).\n\n"
    "<i>Отмена — кнопка «К параметрам» ниже.</i>",
)
chunk = chunk.replace(
    "❌ Отмена: <code>/cancel</code>",
    "<i>Отмена — кнопка ниже.</i>",
)
chunk = chunk.replace(
    "❌ Отмена: кнопка ниже или <code>/cancel</code>",
    "Отмена — кнопка «Назад» ниже.",
)
chunk = chunk.replace("❌ Отмена: /cancel", "Отмена — кнопка «Назад» ниже.")
Path("bot/handlers/_neuro_proc.txt").write_text(chunk, encoding="utf-8")
print("lines", len(chunk.splitlines()))
