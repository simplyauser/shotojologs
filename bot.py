import asyncio
import enum
import io
import os
import traceback
import warnings
from datetime import datetime, timezone, timedelta

import discord

# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════
TOKEN     = os.environ["TOKEN"]
GUILD_ID  = int(os.environ["GUILD_ID"])
LOGS_ID   = int(os.environ["LOGS_CHANNEL_ID"])
LOG_SENT  = os.getenv("LOG_SENT_MESSAGES", "0") == "1"
MSG_CACHE = int(os.getenv("MSG_CACHE", "10000"))

GREEN, RED, ORANGE, BLUE, GREY = 0x57F287, 0xED4245, 0xFAA61A, 0x5865F2, 0x95A5A6

# ════════════════════════════════════════════════════════════════
#  RAID CONFIG
# ════════════════════════════════════════════════════════════════
RAID_LOG_ID      = 1566453272959778866
ALERT_ID         = 1566453465129943181
MUTED_ROLE_ID    = 1560089046842216450
ALERT_ROLE_ID    = 1566238799002992640   # rôle pingé dans #alertes
FONDATEUR_ID     = 1419320803199029402   # mention dans les messages de ban / DM
FONDATION_ID     = 1558327588710121554   # contact fondation pour l'avertissement

MUTE_LIMIT = 2;  MUTE_WINDOW = 5  * 60   # 2 actions en 5 min
DEAF_LIMIT = 2;  DEAF_WINDOW = 5  * 60   # 2 actions en 5 min
DC_LIMIT   = 5;  DC_WINDOW   = 15 * 60   # 5 actions en 15 min

_mute_track:  dict[int, list[float]] = {}
_deaf_track:  dict[int, list[float]] = {}
_dc_track:    dict[int, list[float]] = {}
_sanctioned:  set[int] = set()
# Sauvegarde des rôles avant dérank  { member_id: [role_id, ...] }
_saved_roles: dict[int, list[int]]  = {}

# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════
def now():
    return datetime.now(timezone.utc)

def trunc(s, n=1000):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "..."

def ts(dt):
    t = int(dt.timestamp())
    return f"<t:{t}:F> (<t:{t}:R>)"

def who(u):
    if u is None:
        return "Inconnu"
    if isinstance(u, discord.Object):
        return f"`{u.id}`"
    bot = " 🤖 **BOT**" if getattr(u, "bot", False) else ""
    return f"{u.mention} (`{u}` `{u.id}`){bot}"

def link(channel_id, message_id):
    return f"https://discord.com/channels/{GUILD_ID}/{channel_id}/{message_id}"

def role_mention(r):
    return f"<@&{r.id}>"

def fmt_simple(v):
    if v is None: return "vide"
    if isinstance(v, bool): return "✅" if v else "❌"
    if isinstance(v, discord.Colour): return f"`{v}`"
    if isinstance(v, discord.Asset): return f"[lien]({v.url})"
    if isinstance(v, datetime): return f"<t:{int(v.timestamp())}:F>"
    if isinstance(v, timedelta): return f"`{int(v.total_seconds())}s`"
    if isinstance(v, enum.Enum): return f"`{v.name}`"
    if isinstance(v, discord.Object): return f"`{v.id}`"
    if isinstance(v, (list, tuple, set)): return ", ".join(fmt_simple(x) for x in v) or "vide"
    if hasattr(v, "mention"): return v.mention
    return f"`{trunc(v, 150)}`" if str(v) != "" else "vide"

def fmt_target(t):
    if t is None: return None
    if isinstance(t, discord.Object): return f"ID `{t.id}`"
    if isinstance(t, discord.Invite): return t.url
    mention = getattr(t, "mention", None)
    name = getattr(t, "name", None)
    tid = getattr(t, "id", None)
    parts = []
    if mention: parts.append(mention)
    if name and str(name) not in (mention or ""): parts.append(f"`{name}`")
    if tid: parts.append(f"(`{tid}`)")
    return trunc(" ".join(parts) or str(t), 300)

def fmt_extra(extra):
    if extra is None: return None
    if hasattr(extra, "mention"): return extra.mention
    try:
        items = [f"**{k}** : {fmt_simple(v)}" for k, v in vars(extra).items() if v is not None and not k.startswith("_")]
        if items: return trunc("\n".join(items), 500)
    except Exception: pass
    return trunc(extra, 300)

def perm_set(p):
    return {n for n, v in p if v} if p is not None else set()

def perms_diff(bv, av):
    b, a = perm_set(bv), perm_set(av)
    out = []
    if a - b: out.append("+ " + ", ".join(f"`{x}`" for x in sorted(a - b)))
    if b - a: out.append("- " + ", ".join(f"`{x}`" for x in sorted(b - a)))
    return " | ".join(out)

def ow_target(t):
    if isinstance(t, discord.Object):
        tp = getattr(t, "type", None)
        return f"<@&{t.id}>" if tp is discord.Role else f"<@{t.id}>"
    return t.mention if hasattr(t, "mention") else f"`{t.id}`"

def ow_states(ow):
    return {n: v for n, v in ow if v is not None} if ow is not None else {}

SYM = {True: "✅", False: "❌", None: "➖"}

def overwrites_diff(bv, av):
    bd = {t.id: (t, o) for t, o in (bv or [])}
    ad = {t.id: (t, o) for t, o in (av or [])}
    lines = []
    for tid in list(dict.fromkeys(list(bd) + list(ad))):
        tb, ob = bd.get(tid, (None, None))
        ta, oa = ad.get(tid, (None, None))
        target = ta or tb
        sb, sa = ow_states(ob), ow_states(oa)
        diffs = [f"`{n}` {SYM[sb.get(n)]}➜{SYM[sa.get(n)]}" for n in sorted(set(sb) | set(sa)) if sb.get(n) != sa.get(n)]
        if diffs:
            lines.append(f"{ow_target(target)} : " + ", ".join(diffs))
    return "\n".join(lines)

def changes_chunks(entry):
    try:
        b, a = dict(iter(entry.before)), dict(iter(entry.after))
    except Exception:
        return []
    lines = []
    for k in list(dict.fromkeys(list(b) + list(a))):
        bv, av = b.get(k), a.get(k)
        try:
            if k == "roles" and entry.action.name == "member_role_update":
                if bv: lines.append("Retires : " + ", ".join(role_mention(r) for r in bv))
                if av: lines.append("Ajoutes : " + ", ".join(role_mention(r) for r in av))
            elif isinstance(bv, discord.Permissions) or isinstance(av, discord.Permissions):
                d = perms_diff(bv, av)
                if d: lines.append(f"**{k}** : {d}")
            elif k == "overwrites":
                d = overwrites_diff(bv, av)
                if d: lines.append(f"**permissions du salon** :\n{d}")
            else:
                lines.append(f"**{k}** : {fmt_simple(bv)} -> {fmt_simple(av)}")
        except Exception:
            lines.append(f"**{k}** : (illisible)")
    chunks, cur = [], ""
    for line in lines:
        line = trunc(line, 900)
        if len(cur) + len(line) + 1 > 1000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur: chunks.append(cur)
    return chunks[:4]

# ════════════════════════════════════════════════════════════════
#  ENVOI DES LOGS
# ════════════════════════════════════════════════════════════════
queue: asyncio.Queue = asyncio.Queue()

def emit(embed, file=None):
    if embed.timestamp is None:
        embed.timestamp = now()
    queue.put_nowait((embed, file))

async def sender():
    channel, pending = None, None
    while True:
        embed, file = pending or await queue.get()
        pending = None
        embeds, size = [embed], len(embed)
        if file is None:
            while len(embeds) < 10:
                try:
                    nxt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt[1] is not None or size + len(nxt[0]) > 5500:
                    pending = nxt
                    break
                embeds.append(nxt[0])
                size += len(nxt[0])
        for _ in range(3):
            try:
                if channel is None:
                    channel = client.get_channel(LOGS_ID) or await client.fetch_channel(LOGS_ID)
                await channel.send(embeds=embeds, file=file, allowed_mentions=discord.AllowedMentions.none())
                break
            except Exception:
                traceback.print_exc()
                if file is not None: file = None
                await asyncio.sleep(3)
        await asyncio.sleep(0.4)

# ════════════════════════════════════════════════════════════════
#  RAID DETECTION
# ════════════════════════════════════════════════════════════════
def _ago(dt: datetime) -> str:
    if dt is None:
        return "inconnu"
    delta = now() - dt
    days  = delta.days
    if days >= 365: return f"il y a {days // 365} an(s)"
    if days >= 30:  return f"il y a {days // 30} mois"
    if days >= 1:   return f"il y a {days} jour(s)"
    h = int(delta.total_seconds() // 3600)
    if h >= 1:      return f"il y a {h}h"
    m = int(delta.total_seconds() // 60)
    return f"il y a {m}min" if m > 0 else "à l'instant"

def _add_event(store: dict, uid: int, window: int) -> int:
    t = now().timestamp()
    store.setdefault(uid, [])
    store[uid].append(t)
    store[uid] = [x for x in store[uid] if t - x <= window]
    return len(store[uid])

# ────────────────────────────────────────────────────────────────
#  VUE INTERACTIVE 🎛️ boutons de l'alerte raidlogs
# ────────────────────────────────────────────────────────────────
class RaidAlertView(discord.ui.View):
    """Boutons attachés à l'alerte dans #alertes."""

    def __init__(self, member_id: int, member_name: str, saved_role_ids: list[int]):
        super().__init__(timeout=None)   # persistant jusqu'au redémarrage
        self.member_id      = member_id
        self.member_name    = member_name
        self.saved_role_ids = saved_role_ids

    # ── Bouton 1 : Prendre en charge ────────────────────────────
    @discord.ui.button(
        label="Prendre en charge",
        style=discord.ButtonStyle.primary,
        emoji="✉️",
        custom_id="raid_dm",
    )
    async def btn_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild  = interaction.guild
        member = guild.get_member(self.member_id) if guild else None

        if member is None:
            await interaction.followup.send("❌ Impossible de trouver ce membre sur le serveur.", ephemeral=True)
            return

        handler = interaction.user  # la personne qui a cliqué

        try:
            dm_embed = discord.Embed(
                title="⚠️ Action requise - Serveur Shotojo",
                color=0xFAA61A,
                description=(
                    f"Bonjour {member.mention},\n\n"
                    f"Un **Fondateur** du serveur te contacte suite à une détection automatique de **Raidlogs**.\n"
                    f"Tes permissions ont été temporairement suspendues.\n\n"
                    f"Pour récupérer tes rôles et accès sur le serveur, contacte rapidement "
                    f"{handler.mention} en message privé.\n\n"
                    f"> Plus tu attends, plus la procédure sera longue."
                ),
            )
            dm_embed.set_footer(text="Shotojo • Système de surveillance automatique")
            dm_embed.timestamp = now()
            await member.send(embed=dm_embed)
            await interaction.followup.send(
                f"✅ Message privé envoyé à **{discord.utils.escape_markdown(str(member))}**.\n"
                f"Le membre a été invité à te contacter en MP.",
                ephemeral=True,
            )

            # Log dans #raidlogs
            log_ch = guild.get_channel(RAID_LOG_ID)
            if log_ch:
                log_e = discord.Embed(
                    title="✉️ Prise en charge - Raidlogs",
                    color=0x5865F2,
                    description=(
                        f"{member.mention} (`{member.id}`) a reçu un DM de prise en charge.\n"
                        f"Pris en charge par {handler.mention}.\n"
                        f"Le membre a été invité à contacter {handler.mention} en MP."
                    ),
                )
                log_e.timestamp = now()
                await log_ch.send(embed=log_e)

        except discord.Forbidden:
            await interaction.followup.send("❌ Impossible d'envoyer un DM à cet utilisateur (DMs fermés ou bloqués).", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)

    # ── Bouton 2 : Bannir ───────────────────────────────────────
    @discord.ui.button(
        label="Bannir l'utilisateur",
        style=discord.ButtonStyle.danger,
        emoji="🔨",
        custom_id="raid_ban",
    )
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ Serveur introuvable.", ephemeral=True)
            return

        ban_reason = "Raidlogs, bannissement immédiat décidé par un Fondateur."
        ban_dm_text = (
            f"Tu as été banni(e) de **Shotojo 🌸 #🇯🇵** pour la raison suivante : "
            f"`Raidlogs, bannissement immédiat décidé par un Fondateur`.\n\n"
            f"Si tu veux demander pardon viens ici : https://discord.gg/rAjayCuyyu"
        )
        try:
            # Envoyer le DM AVANT le ban (après le ban on ne peut plus DM)
            member = guild.get_member(self.member_id)
            if member:
                try:
                    await member.send(ban_dm_text)
                except discord.Forbidden:
                    pass  # DMs fermés, on ban quand même
                except Exception:
                    traceback.print_exc()

            await guild.ban(
                discord.Object(id=self.member_id),
                reason=ban_reason,
                delete_message_days=0,
            )
            await interaction.followup.send(
                f"✅ **{discord.utils.escape_markdown(self.member_name)}** (`{self.member_id}`) a été banni et notifié en MP.",
                ephemeral=True,
            )

            # Nettoyage mémoire
            _sanctioned.discard(self.member_id)
            _saved_roles.pop(self.member_id, None)

            # Log dans #raidlogs
            log_ch = guild.get_channel(RAID_LOG_ID)
            if log_ch:
                log_e = discord.Embed(
                    title="🔨 Bannissement - Raidlogs",
                    color=0xED4245,
                    description=(
                        f"<@{self.member_id}> (`{self.member_id}`) a été **banni** suite à un raidlogs.\n"
                        f"Décision prise par {interaction.user.mention}.\n\n"
                        f"**Raison :** {ban_reason}"
                    ),
                )
                log_e.timestamp = now()
                await log_ch.send(embed=log_e)

            # Désactiver les boutons du message d'alerte
            for item in self.children:
                item.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

        except discord.Forbidden:
            await interaction.followup.send("❌ Je n'ai pas la permission de bannir cet utilisateur.", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("❌ Une erreur est survenue lors du bannissement.", ephemeral=True)

    # ── Bouton 3 : Réattribuer les rôles ────────────────────────
    @discord.ui.button(
        label="Réattribuer les rôles",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="raid_restore",
    )
    async def btn_restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild  = interaction.guild
        member = guild.get_member(self.member_id) if guild else None

        if member is None:
            await interaction.followup.send("❌ Ce membre n'est plus sur le serveur.", ephemeral=True)
            return

        muted_role = guild.get_role(MUTED_ROLE_ID)
        saved      = self.saved_role_ids or []

        # Retirer le rôle muet
        try:
            if muted_role and muted_role in member.roles:
                await member.remove_roles(muted_role, reason="[RAID] Réattribution manuelle des rôles")
        except Exception:
            traceback.print_exc()

        # Remettre les rôles sauvegardés
        roles_to_add = [guild.get_role(rid) for rid in saved if guild.get_role(rid) is not None]
        roles_to_add = [r for r in roles_to_add if not r.managed and r.id != guild.id]
        restored_ok  = False
        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="[RAID] Réattribution manuelle des rôles")
            restored_ok = True
        except Exception:
            traceback.print_exc()

        # DM d'avertissement stylé
        try:
            warn_embed = discord.Embed(
                title="⚠️ Avertissement officiel - Shotojo",
                color=0xFAA61A,
                description=(
                    f"Bonjour {member.mention},\n\n"
                    f"Tes rôles et permissions viennent d'être **restaurés** sur le serveur.\n\n"
                    f"Cependant, tu as été détecté par notre système de surveillance automatique "
                    f"(**Raidlogs**) pour **abus de permissions**.\n\n"
                    f"\n> 🚫 **Ne répète pas ce comportement.**\n"
                    f"> Tout nouvel abus entraînera un **bannissement immédiat et définitif** du serveur, "
                    f"sans possibilité d'appel.\n\n"
                    f"En cas de question, contacte <@{FONDATION_ID}> et un membre de la fondation te répondra."
                ),
            )
            warn_embed.set_footer(text="Shotojo • Système de surveillance automatique")
            warn_embed.timestamp = now()
            await member.send(embed=warn_embed)
        except discord.Forbidden:
            pass  # DMs fermés, on continue quand même
        except Exception:
            traceback.print_exc()

        # Remettre sous surveillance : retirer de _sanctioned ET reset les timers
        _sanctioned.discard(self.member_id)
        _mute_track.pop(self.member_id, None)
        _deaf_track.pop(self.member_id, None)
        _dc_track.pop(self.member_id, None)
        # Conserver les rôles sauvegardés pour un éventuel nouvel incident
        # (ils seront réécrasés par _sanction si ça recommence)

        roles_list = ", ".join(r.mention for r in roles_to_add) if roles_to_add else "aucun rôle à restaurer"
        await interaction.followup.send(
            f"✅ Rôles restaurés pour **{discord.utils.escape_markdown(str(member))}** : {roles_list}\n"
            f"Il est de nouveau sous surveillance Raidlogs.",
            ephemeral=True,
        )

        # Log dans #raidlogs
        log_ch = guild.get_channel(RAID_LOG_ID)
        if log_ch:
            log_e = discord.Embed(
                title="🔓 Rôles réattribués ✅ Raidlogs",
                color=0x57F287,
                description=(
                    f"{member.mention} (`{member.id}`) a retrouvé ses permissions.\n"
                    f"Décision prise par {interaction.user.mention}.\n"
                    f"Rôles restaurés : {roles_list}\n\n"
                    f"⚠️ Ce membre est de nouveau surveillé par le système Raidlogs."
                ),
            )
            log_e.timestamp = now()
            await log_ch.send(embed=log_e)

        # Désactiver les boutons du message d'alerte
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass


async def _sanction(
    guild: discord.Guild,
    member: discord.Member,
    emoji: str,
    label: str,
    color: int,
    count: int,
    window_min: int,
    limit: int,
):
    if member.id in _sanctioned:
        return
    _sanctioned.add(member.id)

    # Rang le plus haut avant dérank
    top = sorted(
        [r for r in member.roles if r.id != guild.id],
        key=lambda r: r.position,
        reverse=True,
    )
    rang = top[0].name if top else "aucun"

    muted_role = guild.get_role(MUTED_ROLE_ID)
    to_remove  = [r for r in member.roles if r.id != guild.id and not r.managed]

    # ── Sauvegarder les rôles pour la réattribution future ──────
    _saved_roles[member.id] = [r.id for r in to_remove]

    derank_ok = mute_ok = False
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"[RAID] {label}")
        derank_ok = True
    except Exception:
        traceback.print_exc()
    try:
        if muted_role:
            await member.add_roles(muted_role, reason=f"[RAID] {label}")
        mute_ok = True
    except Exception:
        traceback.print_exc()

    if derank_ok and mute_ok:
        riposte = "✅ Auteur dérank et mute"
    else:
        parts = (["✅ Dérank effectué"] if derank_ok else ["❌ Dérank échoué"]) + \
                (["✅ Mute effectué"]   if mute_ok   else ["❌ Mute échoué"])
        riposte = "\n".join(parts)

    roles_saved_fmt = (
        ", ".join(f"<@&{rid}>" for rid in _saved_roles.get(member.id, []))
        or "aucun"
    )

    # ════════════════════════════════════════════════════════════
    #  EMBED RAIDLOGS (salon #raidlogs 🔒 log discret)
    # ════════════════════════════════════════════════════════════
    log_embed = discord.Embed(
        title=f"{emoji}  {label}",
        color=color,
        description=(
            f"👤 **Auteur** : {member.mention}  {discord.utils.escape_markdown(str(member))}\n"
            f"`{member.id}`  •  rang : {rang}\n"
            f"compte créé {_ago(member.created_at)}  •  arrivé {_ago(member.joined_at)}\n\n"
            f"⚡ **Actions** : {count} en {window_min} minutes\n"
            f"limite : {limit}\n\n"
            f"**Riposte**\n"
            f"{riposte}"
        ),
    )
    log_embed.set_thumbnail(url=member.display_avatar.url)
    log_embed.set_footer(text="Raidlogs • Surveillance automatique")
    log_embed.timestamp = now()

    log_view = discord.ui.View()
    log_view.add_item(discord.ui.Button(
        label="Profil de l'auteur",
        url=f"https://discord.com/users/{member.id}",
        style=discord.ButtonStyle.link,
        emoji="🔗",
    ))

    log_ch = guild.get_channel(RAID_LOG_ID)
    if log_ch:
        try:
            await log_ch.send(embed=log_embed, view=log_view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            traceback.print_exc()

    # ════════════════════════════════════════════════════════════
    #  EMBED ALERTE (salon #alertes 🚨 stylé + ping + boutons)
    # ════════════════════════════════════════════════════════════
    alert_embed = discord.Embed(
        title=f"🚨 ALERTE RAIDLOGS - {label}",
        color=0xFF0000,
        description=(
            f">>> 🔴 **Menace neutralisée automatiquement par le système de protection.**\n\n"
            f"**👤 Membre visé**\n"
            f"{member.mention}  •  `{discord.utils.escape_markdown(str(member))}`\n"
            f"`{member.id}`\n\n"
            f"**📊 Activité détectée**\n"
            f"{emoji} **{label}**\n"
            f"**{count} actions** en **{window_min} min** (limite : {limit})\n\n"
            f"**🕐 Infos compte**\n"
            f"Créé : {_ago(member.created_at)}  •  Arrivé : {_ago(member.joined_at)}\n"
            f"Rang le plus haut : **{rang}**\n\n"
            f"**⚙️ Sanction appliquée**\n"
            f"{riposte}\n"
            f"Rôles mis de côté : {roles_saved_fmt}\n\n"
            f"*Sélectionnez une action ci-dessous pour traiter ce cas.*"
        ),
    )
    alert_embed.set_thumbnail(url=member.display_avatar.url)
    alert_embed.set_author(
        name="Système Raidlogs - Alerte automatique",
        icon_url=guild.icon.url if guild.icon else discord.Embed.Empty,
    )
    alert_embed.set_footer(text=f"ID membre : {member.id} - Shotojo Surveillance")
    alert_embed.timestamp = now()

    alert_view = RaidAlertView(
        member_id=member.id,
        member_name=str(member),
        saved_role_ids=_saved_roles.get(member.id, []),
    )
    # Lien profil en bouton non-interactif (URL)
    alert_view.add_item(discord.ui.Button(
        label="Voir le profil",
        url=f"https://discord.com/users/{member.id}",
        style=discord.ButtonStyle.link,
        emoji="🔗",
    ))

    alert_ch = guild.get_channel(ALERT_ID)
    if alert_ch:
        try:
            await alert_ch.send(
                content=f"<@&{ALERT_ROLE_ID}>",
                embed=alert_embed,
                view=alert_view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except Exception:
            traceback.print_exc()


async def _check_raid(entry: discord.AuditLogEntry):
    try:
        guild  = entry.guild
        user   = entry.user
        if user is None or user.bot:
            return
        member = guild.get_member(user.id)
        if member is None:
            return

        name = entry.action.name

        if name == "member_update":
            try:
                b = dict(iter(entry.before))
                a = dict(iter(entry.after))
            except Exception:
                return
            # Mute serveur (clic droit → muter / démuter)
            if "mute" in b or "mute" in a:
                n = _add_event(_mute_track, user.id, MUTE_WINDOW)
                if n >= MUTE_LIMIT:
                    await _sanction(guild, member, "🔇", "Trop de mutes", BLUE, n, 5, MUTE_LIMIT)
            # Sourd serveur / casque (clic droit → rendre sourd)
            if "deaf" in b or "deaf" in a:
                n = _add_event(_deaf_track, user.id, DEAF_WINDOW)
                if n >= DEAF_LIMIT:
                    await _sanction(guild, member, "🎧", "Trop de mutes casque", BLUE, n, 5, DEAF_LIMIT)

        elif name == "member_disconnect":
            n = _add_event(_dc_track, user.id, DC_WINDOW)
            if n >= DC_LIMIT:
                await _sanction(guild, member, "🔌", "Trop de déconnexions", ORANGE, n, 15, DC_LIMIT)

    except Exception:
        traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  CLIENT
# ════════════════════════════════════════════════════════════════
class LogClient(discord.Client):
    async def setup_hook(self):
        asyncio.create_task(sender())

client = LogClient(
    intents=discord.Intents.all(),
    max_messages=MSG_CACHE,
    status=discord.Status.online,
    activity=discord.Activity(type=discord.ActivityType.watching, name="Shotojo 🌸 #🇯🇵 • Surveillance"),
)

_started = False

@client.event
async def on_ready():
    global _started
    print(f"✅ {client.user} connecté (ID: {client.user.id})")
    if _started: return
    _started = True
    guild = client.get_guild(GUILD_ID)

    # Embed logs
    e = discord.Embed(title="✅ Bot de logs en ligne", color=GREEN)
    if guild:
        e.description = f"Surveillance du serveur **{guild.name}** démarrée."
        if not guild.me.guild_permissions.view_audit_log:
            e.add_field(name="⚠️ Permission manquante",
                value="Il me faut **Voir les logs du serveur** pour savoir qui fait les actions.", inline=False)
    else:
        e.color = RED
        e.description = "⚠️ Le bot n'est pas dans le serveur configuré (GUILD_ID)."
    emit(e)

    # Embed alerte démarrage dans #alertes
    if guild:
        alert_ch = guild.get_channel(ALERT_ID)
        if alert_ch:
            try:
                startup_embed = discord.Embed(
                    title="🟢 Système de protection en ligne",
                    color=0x57F287,
                    description=(
                        f">>> ✅ **Le système Raidlogs est opérationnel.**\n\n"
                        f"**🛡️ Serveur surveillé :** {guild.name}\n"
                        f"**🔍 Détection active :**\n"
                        f"🔇 Mutes abusifs : {MUTE_LIMIT} en {MUTE_WINDOW // 60} min\n"
                        f"🎧 Sourds abusifs : {DEAF_LIMIT} en {DEAF_WINDOW // 60} min\n"
                        f"🔌 Déconnexions abusives : {DC_LIMIT} en {DC_WINDOW // 60} min\n\n"
                        f"*Toute infraction déclenchera une alerte dans ce salon.*"
                    ),
                )
                startup_embed.set_footer(text="Shotojo • Système de surveillance automatique")
                startup_embed.timestamp = now()
                await alert_ch.send(embed=startup_embed)
            except Exception:
                traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  MESSAGES : suppression
# ════════════════════════════════════════════════════════════════
async def find_deleter(guild, author_id, channel_id):
    try:
        async for en in guild.audit_logs(limit=10, action=discord.AuditLogAction.message_delete):
            if (now() - en.created_at).total_seconds() > 15: break
            ch = getattr(en.extra, "channel", None)
            if en.target is not None and en.target.id == author_id and getattr(ch, "id", None) == channel_id:
                return en.user
    except discord.Forbidden:
        return "forbidden"
    except Exception:
        pass
    return None

def deleter_text(d):
    if d == "forbidden": return "Permission *Voir les logs du serveur* manquante"
    if d: return who(d)
    return "Lui-meme (ou suppression non tracee par Discord)"

def msg_details(e, msg):
    if msg.attachments:
        e.add_field(name="📎 Pieces jointes",
            value=trunc("\n".join(f"[{a.filename}]({a.url})" for a in msg.attachments), 1000), inline=False)
    if msg.stickers:
        e.add_field(name="Stickers", value=", ".join(s.name for s in msg.stickers), inline=False)
    if msg.embeds:
        e.add_field(name="Embeds", value=f"{len(msg.embeds)} embed(s)", inline=True)
    if msg.reference and msg.reference.message_id:
        e.add_field(name="En reponse a", value=f"[message]({link(msg.reference.channel_id, msg.reference.message_id)})", inline=True)

async def handle_delete(payload):
    try:
        msg = payload.cached_message
        guild = client.get_guild(GUILD_ID)
        e = discord.Embed(title="🗑️ Message supprime", color=RED)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        if msg:
            await asyncio.sleep(1.5)
            deleter = await find_deleter(guild, msg.author.id, payload.channel_id) if guild else None
            e.add_field(name="✍️ Auteur", value=who(msg.author), inline=True)
            e.add_field(name="🗑️ Supprime par", value=deleter_text(deleter), inline=False)
            e.add_field(name="📅 Envoye", value=ts(msg.created_at), inline=True)
            e.add_field(name="🆔 Message", value=f"`{msg.id}`", inline=True)
            e.add_field(name="💬 Contenu", value=trunc(msg.content, 1000) or "(aucun texte)", inline=False)
            msg_details(e, msg)
            e.set_thumbnail(url=msg.author.display_avatar.url)
        else:
            e.add_field(name="🆔 Message", value=f"`{payload.message_id}`", inline=True)
            e.add_field(name="ℹ️", value="Contenu et auteur inconnus (message pas en cache).", inline=False)
        emit(e)
    except Exception:
        traceback.print_exc()

@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID: return
    asyncio.create_task(handle_delete(payload))

async def handle_bulk(payload):
    try:
        guild = client.get_guild(GUILD_ID)
        await asyncio.sleep(1.5)
        deleter = None
        try:
            async for en in guild.audit_logs(limit=5, action=discord.AuditLogAction.message_bulk_delete):
                if (now() - en.created_at).total_seconds() > 15: break
                if en.target is not None and en.target.id == payload.channel_id:
                    deleter = en.user; break
        except discord.Forbidden:
            deleter = "forbidden"
        except Exception:
            pass
        e = discord.Embed(title="🧹 Suppression en masse", color=RED)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        e.add_field(name="🔢 Messages", value=str(len(payload.message_ids)), inline=True)
        e.add_field(name="🗑️ Supprime par", value=deleter_text(deleter) if deleter else "Inconnu", inline=False)
        cached = sorted(payload.cached_messages, key=lambda m: m.created_at)
        file = None
        if cached:
            e.add_field(name="💾 En cache", value=f"{len(cached)} message(s) (voir fichier joint)", inline=False)
            text = "\n".join(
                f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author} ({m.author.id}): {m.content}"
                + (" | PJ: " + ", ".join(a.url for a in m.attachments) if m.attachments else "")
                for m in cached)
            file = discord.File(io.BytesIO(text.encode("utf-8")), filename="messages_supprimes.txt")
        emit(e, file)
    except Exception:
        traceback.print_exc()

@client.event
async def on_raw_bulk_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID: return
    asyncio.create_task(handle_bulk(payload))

# ════════════════════════════════════════════════════════════════
#  MESSAGES : modification / envoi / reactions
# ════════════════════════════════════════════════════════════════
@client.event
async def on_raw_message_edit(payload):
    try:
        if payload.guild_id != GUILD_ID or payload.channel_id == LOGS_ID: return
        if "content" not in payload.data: return
        after = payload.data["content"]
        old = payload.cached_message
        if old is not None and old.content == after: return
        e = discord.Embed(title="✏️ Message modifie", color=ORANGE)
        author = old.author if old else None
        if author is None and payload.data.get("author"):
            uid = int(payload.data["author"]["id"])
            author = client.get_user(uid) or discord.Object(id=uid)
        e.add_field(name="✍️ Auteur", value=who(author) if author else "Inconnu", inline=True)
        e.add_field(name="📍 Salon", value=f"<#{payload.channel_id}>", inline=True)
        e.add_field(name="🔗 Lien", value=f"[Aller au message]({link(payload.channel_id, payload.message_id)})", inline=True)
        e.add_field(name="Avant", value=trunc(old.content, 1000) or "(vide)" if old else "(inconnu, pas en cache)", inline=False)
        e.add_field(name="Apres", value=trunc(after, 1000) or "(vide)", inline=False)
        if isinstance(author, (discord.User, discord.Member)):
            e.set_thumbnail(url=author.display_avatar.url)
        emit(e)
    except Exception:
        traceback.print_exc()

@client.event
async def on_message(message):
    try:
        if not LOG_SENT or message.guild is None or message.guild.id != GUILD_ID or message.channel.id == LOGS_ID: return
        e = discord.Embed(title="💬 Message envoye", color=BLUE if not message.author.bot else GREY)
        e.add_field(name="✍️ Auteur", value=who(message.author), inline=True)
        e.add_field(name="📍 Salon", value=message.channel.mention, inline=True)
        e.add_field(name="🔗 Lien", value=f"[Aller au message]({message.jump_url})", inline=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                inter = message.interaction
            if inter is not None:
                e.add_field(name="Commande slash", value=f"`/{inter.name}` par {inter.user.mention}", inline=False)
        except Exception:
            pass
        e.add_field(name="💬 Contenu", value=trunc(message.content, 1000) or "(aucun texte)", inline=False)
        msg_details(e, message)
        e.set_thumbnail(url=message.author.display_avatar.url)
        emit(e)
    except Exception:
        traceback.print_exc()

# Réactions : non loggées (trop fréquentes, peu utiles)

# ════════════════════════════════════════════════════════════════
#  MEMBRES
# ════════════════════════════════════════════════════════════════
@client.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID: return
    e = discord.Embed(title="🤖 Bot arrive" if member.bot else "📥 Membre arrive", color=GREEN)
    e.add_field(name="👤 Membre", value=who(member), inline=False)
    e.add_field(name="📅 Compte cree", value=ts(member.created_at), inline=False)
    if (now() - member.created_at).days < 7:
        e.add_field(name="⚠️ Compte recent", value=f"Cree il y a {(now() - member.created_at).days} jour(s)", inline=False)
    e.add_field(name="👥 Membres", value=str(member.guild.member_count), inline=True)
    e.set_thumbnail(url=member.display_avatar.url)
    emit(e)

@client.event
async def on_member_remove(member):
    if member.guild.id != GUILD_ID: return
    e = discord.Embed(title="📤 Membre parti", color=RED)
    e.add_field(name="👤 Membre", value=who(member), inline=False)
    if member.joined_at:
        e.add_field(name="📅 Avait rejoint", value=ts(member.joined_at), inline=False)
    roles = [role_mention(r) for r in member.roles if r.id != member.guild.id]
    if roles:
        e.add_field(name="Roles", value=trunc(", ".join(roles), 1000), inline=False)
    e.add_field(name="👥 Membres", value=str(member.guild.member_count), inline=True)
    e.set_thumbnail(url=member.display_avatar.url)
    emit(e)

@client.event
async def on_user_update(before, after):
    guild = client.get_guild(GUILD_ID)
    if not guild or not guild.get_member(after.id): return
    lines = []
    if before.name != after.name:
        lines.append(f"**Pseudo** : `{before.name}` -> `{after.name}`")
    if before.global_name != after.global_name:
        lines.append(f"**Nom d'affichage** : `{before.global_name}` -> `{after.global_name}`")
    if before.avatar != after.avatar:
        lines.append(f"**Avatar** : [avant]({before.display_avatar.url}) -> [apres]({after.display_avatar.url})")
    if lines:
        e = discord.Embed(title="Profil modifie", color=ORANGE, description=f"{who(after)}\n" + "\n".join(lines))
        e.set_thumbnail(url=after.display_avatar.url)
        emit(e)

@client.event
async def on_member_update(before, after):
    if after.guild.id != GUILD_ID: return
    if before.guild_avatar != after.guild_avatar:
        new = after.guild_avatar.url if after.guild_avatar else None
        old = before.guild_avatar.url if before.guild_avatar else None
        e = discord.Embed(title="Avatar de serveur modifie", color=ORANGE,
            description=f"{who(after)}\nAvant : {old or 'aucun'}\nApres : {new or 'aucun'}")
        emit(e)
    if before.premium_since != after.premium_since:
        boost = after.premium_since is not None
        emit(discord.Embed(title="Boost " + ("ajoute" if boost else "retire"), color=BLUE if boost else GREY,
            description=who(after)))

# ════════════════════════════════════════════════════════════════
#  VOCAL
# ════════════════════════════════════════════════════════════════
VOICE_FLAGS = [
    ("self_mute", "🎙️ Micro coupe (par lui)"), ("self_deaf", "🎧 Casque coupe (par lui)"),
    ("mute", "🔇 Mute serveur"), ("deaf", "🔕 Sourd serveur"),
    ("self_stream", "📺 Partage d'ecran"), ("self_video", "📷 Camera"), ("suppress", "🚫 Supprime (stage)"),
]

# Vocal : connexions/déconnexions non loggées (trop fréquentes, peu utiles)
# Le raid sur les déconnexions forcées est toujours détecté via on_audit_log_entry_create

# ════════════════════════════════════════════════════════════════
#  AUTOMOD
# ════════════════════════════════════════════════════════════════
@client.event
async def on_automod_action(execution):
    try:
        if execution.guild_id != GUILD_ID: return
        e = discord.Embed(title="🛑 AutoMod declenche", color=RED)
        e.add_field(name="👤 Membre", value=f"<@{execution.user_id}> (`{execution.user_id}`)", inline=True)
        if execution.channel_id:
            e.add_field(name="📍 Salon", value=f"<#{execution.channel_id}>", inline=True)
        e.add_field(name="Action", value=f"`{execution.action.type.name}`", inline=True)
        e.add_field(name="Regle", value=f"`{execution.rule_id}`", inline=True)
        if execution.matched_keyword:
            e.add_field(name="Mot detecte", value=f"`{trunc(execution.matched_keyword, 100)}`", inline=True)
        if execution.content:
            e.add_field(name="💬 Contenu", value=trunc(execution.content, 1000), inline=False)
        emit(e)
    except Exception:
        traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  LOGS D'AUDIT
# ════════════════════════════════════════════════════════════════
LABELS = {
    "guild_update": ("⚙️", "Serveur modifie"),
    "channel_create": ("📁", "Salon cree"), "channel_update": ("📝", "Salon modifie"), "channel_delete": ("🗑️", "Salon supprime"),
    "overwrite_create": ("🔐", "Permission de salon ajoutee"), "overwrite_update": ("🔐", "Permission de salon modifiee"),
    "overwrite_delete": ("🔐", "Permission de salon retiree"),
    "kick": ("👢", "Membre expulse"), "member_prune": ("🧹", "Purge de membres inactifs"),
    "ban": ("🔨", "Membre banni"), "unban": ("✅", "Membre debanni"),
    "member_update": ("✏️", "Membre modifie (pseudo / timeout / mute)"),
    "member_role_update": ("🎭", "Roles d'un membre modifies"),
    "member_move": ("Membre deplace en vocal", ""), "member_disconnect": ("🔌", "Membre deconnecte du vocal"),
    "bot_add": ("🤖", "Bot ajoute au serveur"),
    "role_create": ("🎭", "Role cree"), "role_update": ("🎭", "Role modifie"), "role_delete": ("🎭", "Role supprime"),
    "invite_create": ("🔗", "Invitation creee"), "invite_update": ("🔗", "Invitation modifiee"), "invite_delete": ("🔗", "Invitation supprimee"),
    "webhook_create": ("Webhook cree", ""), "webhook_update": ("Webhook modifie", ""), "webhook_delete": ("Webhook supprime", ""),
    "emoji_create": ("😀", "Emoji cree"), "emoji_update": ("😀", "Emoji modifie"), "emoji_delete": ("😀", "Emoji supprime"),
    "message_pin": ("📌", "Message epingle"), "message_unpin": ("📌", "Message desepingle"),
    "integration_create": ("Integration ajoutee", ""), "integration_update": ("Integration modifiee", ""), "integration_delete": ("Integration supprimee", ""),
    "stage_instance_create": ("🎤", "Stage cree"), "stage_instance_update": ("🎤", "Stage modifie"), "stage_instance_delete": ("🎤", "Stage supprime"),
    "sticker_create": ("Sticker cree", ""), "sticker_update": ("Sticker modifie", ""), "sticker_delete": ("Sticker supprime", ""),
    "scheduled_event_create": ("📆", "Evenement cree"), "scheduled_event_update": ("📆", "Evenement modifie"), "scheduled_event_delete": ("📆", "Evenement supprime"),
    "thread_create": ("🧵", "Fil cree"), "thread_update": ("🧵", "Fil modifie"), "thread_delete": ("🧵", "Fil supprime"),
    "app_command_permission_update": ("Permissions de commande modifiees", ""),
    "automod_rule_create": ("🛑", "Regle AutoMod creee"), "automod_rule_update": ("🛑", "Regle AutoMod modifiee"), "automod_rule_delete": ("🛑", "Regle AutoMod supprimee"),
    "automod_block_message": ("🛑", "AutoMod : message bloque"), "automod_flag_message": ("🛑", "AutoMod : message signale"),
    "automod_timeout_member": ("🛑", "AutoMod : membre mis en timeout"),
}
RED_ACTIONS = {"kick", "ban", "member_prune", "member_disconnect", "automod_block_message", "automod_timeout_member"}
GREEN_ACTIONS = {"unban", "bot_add"}
SKIP_AUDIT = {
    discord.AuditLogAction.message_delete,
    discord.AuditLogAction.message_bulk_delete,
    # Vocal (connexions, déplacements, états) : trop fréquents
    discord.AuditLogAction.member_move,
    discord.AuditLogAction.member_disconnect,
    # Threads : peu utiles en surveillance
    discord.AuditLogAction.thread_create,
    discord.AuditLogAction.thread_update,
    discord.AuditLogAction.thread_delete,
    # Evénements planifiés : pas pertinent sécurité
    discord.AuditLogAction.scheduled_event_create,
    discord.AuditLogAction.scheduled_event_update,
    discord.AuditLogAction.scheduled_event_delete,
    # Stage : rare et peu critique
    discord.AuditLogAction.stage_instance_create,
    discord.AuditLogAction.stage_instance_update,
    discord.AuditLogAction.stage_instance_delete,
    # Intégrations : pas actionnables
    discord.AuditLogAction.integration_create,
    discord.AuditLogAction.integration_update,
    discord.AuditLogAction.integration_delete,
}

def audit_color(entry):
    n = entry.action.name
    if n in RED_ACTIONS: return RED
    if n in GREEN_ACTIONS: return GREEN
    cat = entry.category
    if cat == discord.AuditLogActionCategory.create: return GREEN
    if cat == discord.AuditLogActionCategory.delete: return RED
    if cat == discord.AuditLogActionCategory.update: return ORANGE
    return GREY

@client.event
async def on_audit_log_entry_create(entry):
    # Raid detection : tourne toujours, indépendamment du filtre SKIP_AUDIT
    if entry.guild.id == GUILD_ID:
        asyncio.create_task(_check_raid(entry))

    try:
        if entry.guild.id != GUILD_ID or entry.action in SKIP_AUDIT: return
        name = entry.action.name
        label_data = LABELS.get(name, ("📋", name.replace("_", " ").capitalize()))
        emoji, label = (label_data[0], label_data[1]) if label_data[1] else ("📋", label_data[0])
        e = discord.Embed(title=f"{emoji} {label}", color=audit_color(entry), timestamp=entry.created_at)
        e.add_field(name="👤 Executant", value=who(entry.user) if entry.user else f"`{entry.user_id}`", inline=True)
        target = fmt_target(entry.target)
        if target: e.add_field(name="Cible", value=target, inline=True)
        if entry.reason: e.add_field(name="📝 Raison", value=trunc(entry.reason, 500), inline=False)
        extra = fmt_extra(entry.extra)
        if extra: e.add_field(name="Details", value=extra, inline=False)
        for i, chunk in enumerate(changes_chunks(entry)):
            e.add_field(name="Changements" if i == 0 else "(suite)", value=chunk, inline=False)
        e.set_footer(text=f"Audit ID {entry.id}")
        emit(e)
    except Exception:
        traceback.print_exc()

# ════════════════════════════════════════════════════════════════
#  LANCEMENT
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    client.run(TOKEN)
