from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InputMediaPhoto
from bot import Bot, Vars, logger, is_admin
from Tools.db import ensure_user, uts, sync
from Tools.ui import btn, esc
from .storage import igrone_error, retry_on_flood
import random
from pyrogram.errors import ListenerTimeout as TimeoutError

users_txt = """<b>⚙️ Your settings</b>
<i>Applied to every file you download.</i>

<b>Files</b>
<b>Type</b>  {type}
<b>Quality</b>  {compress}
<b>Merge</b>  {megre}
<b>Password</b>  {password}

<b>Naming</b>
<b>File name</b>  {file_name} <i>(length {len})</i>
<b>Caption</b>  {caption}
<b>Regex / zfill</b>  {regex}

<b>Look</b>
<b>Thumbnail</b>  {thumb}
<b>Banners</b>  {banner1} · {banner2}
<b>Dump channel</b>  {dump}

<i>ID</i> <code>{id}</code>"""


def _v(value):
  """Settings read as '—' when unset, and user text can't break the card's HTML."""
  if value in (None, "", "None"):
    return "—"
  return f"<code>{esc(value)}</code>"


info_data_text = {
  "caption": (
    "<b>💬 Caption</b>\n<i>Sent with every file.</i>\n\n"
    "<blockquote>HTML works — <code>&lt;b&gt;bold&lt;/b&gt;</code>, <code>&lt;i&gt;italic&lt;/i&gt;</code>…</blockquote>\n\n"
    "<b>Placeholders</b>\n"
    "<code>{manga_title}</code> — manga name\n"
    "<code>{chapter_num}</code> — chapter number\n"
    "<code>{file_name}</code> — file name\n\n"
    "<i>Send your caption now.</i>"),

  "dump": (
    "<b>📥 Dump channel</b>\n<i>A private copy of every file you download.</i>\n\n"
    "<blockquote>Send the channel's username (without @), its numeric ID, or forward a message from it. "
    "The bot must be an admin there.</blockquote>"),

  "megre": (
    "<b>📦 Merge size</b>\n<i>How many chapters go into one file.</i>\n\n"
    "<blockquote>Send a number — e.g. <code>3</code> merges every 3 chapters into a single PDF/CBZ.</blockquote>"),

  "password": (
    "<b>🔐 PDF password</b>\n<i>Your PDFs will ask for it to open.</i>\n\n"
    "<blockquote>Send the password you want to use.</blockquote>"),
}


def get_user_txt(user_id):
  user_id = str(user_id)
  ensure_user(user_id)

  thumbnali = uts[user_id]['setting'].get("thumb", None)

  if thumbnali:
    thumb = thumbnali if thumbnali.startswith("http") else "True"
    thumb = "Constant" if thumbnali == "constant" else thumbnali
  else:
    thumb = thumbnali

  banner1 = uts[user_id]['setting'].get("banner1", None)
  banner2 = uts[user_id]['setting'].get("banner2", None)

  if banner1:
    banner1 = banner1 if banner1.startswith("http") else "True"

  if banner2:
    banner2 = banner2 if banner2.startswith("http") else "True"

  setting = uts[user_id]['setting']
  txt = users_txt.format(
      id=user_id,
      file_name=_v(setting.get("file_name")),
      caption=_v(setting.get("caption")),
      thumb=_v(thumb),
      banner1=_v(banner1 and "set"),
      banner2=_v(banner2 and "set"),
      dump=_v(setting.get("dump")),
      type=_v(setting.get("type")),
      megre=_v(setting.get("megre")),
      regex=_v(setting.get("regex")),
      len=_v(setting.get("file_name_len")),
      password=_v(setting.get("password") and "••••••"),
      compress=_v(setting.get("compress")),
  )
  return txt, thumbnali


async def main_settings(_, message_query, user_id):
  ensure_user(user_id)
  txt, thumbnali = get_user_txt(user_id)

  button = [
      [
          btn("📝 File name", callback_data="ufn"),
          btn("💬 Caption", callback_data="sinfo_caption")
      ],
      [
          btn("🖼 Thumbnail", callback_data="uth"),
          btn("🔣 Regex", callback_data="uregex")
      ],
      [
          btn("🏷 Banners", callback_data="ubn"),
          btn("🗜 Quality", callback_data="u_compress"),
      ],
      [
          btn("🔐 Password", callback_data="sinfo_password"),
          btn("📦 Merge size", callback_data="sinfo_megre")
      ],
      [
          btn("📄 File type", callback_data="u_file_type"),
      ],
  ]
  if not Vars.CONSTANT_DUMP_CHANNEL:
    button[-1].append(
        btn("📥 Dump channel", callback_data="sinfo_dump"))

  button.append([btn("✖ Close", callback_data="close", style=None)])

  if not thumbnali or thumbnali == "constant":
    thumbnali = random.choice(Vars.PICS)

  try:
    await retry_on_flood(message_query.edit_media)(
      InputMediaPhoto(thumbnali, caption=txt),
      reply_markup=InlineKeyboardMarkup(button)
    )
  except Exception:
    await retry_on_flood(message_query.edit_media)(
      InputMediaPhoto(Vars.PICS[-2], caption=txt),
      reply_markup=InlineKeyboardMarkup(button)
    )



@Bot.on_message(filters.command(["us", "user_setting", "user_panel"]))
async def userxsettings(client, message):
  if Vars.IS_PRIVATE:
    if not is_admin(message.chat.id):
      return await message.reply(Vars.PRIVATE_TXT)

  sts = await message.reply("<i>Opening your settings…</i>", quote=True)
  try:
    await main_settings(client, sts, message.from_user.id)
  except Exception as err:
    logger.exception(err)
    await retry_on_flood(sts.edit)(err)



@Bot.on_callback_query(filters.regex("^mus$"))
async def main_user_panel(_, query):
  await igrone_error(query.answer)()
  await main_settings(_, query.message, query.from_user.id)


@Bot.on_callback_query(filters.regex("^sinfo"))
async def user_settings(_, query):
  type = query.data.removeprefix("sinfo_")
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  button = [
    [
      btn("✏️ Set / change", callback_data=f"sset_{type}"),
      btn("🗑 Delete", callback_data=f"sdelete_{type}")
    ],
    [
      btn("⇦ Back", callback_data="mus", style=None)
    ]
  ]
  await igrone_error(query.answer)()
  await retry_on_flood(query.edit_message_reply_markup)(InlineKeyboardMarkup(button))


@Bot.on_callback_query(filters.regex("^sset"))
async def user_settings_set(_, query):
  type = query.data.removeprefix("sset_")
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  try:
    await retry_on_flood(query.edit_message_caption)(info_data_text[type])
    call_ = await _.listen(user_id=int(user_id), timeout=80, filters=filters.text | filters.forwarded)
    if type == "dump":
      if call_.forward_from_chat:
        uts[user_id]['setting']["dump"] = call_.forward_from_chat.id
        sync()
      
      elif call_.text:
        try: uts[user_id]['setting']["dump"] = int(call_.text)
        except: uts[user_id]['setting']["dump"] = call_.text
        sync()
        
    else:
      uts[user_id]['setting'][type] = call_.text
      sync()
    
    await igrone_error(call_.delete)()
  
  except TimeoutError:
    await retry_on_flood(query.answer)("⏰ Timed out.")
  except Exception as err:
    await retry_on_flood(query.answer)(f"⚠️ {err}")
  
  await igrone_error(query.answer)()
  await main_settings(_, query.message, query.from_user.id)
        



@Bot.on_callback_query(filters.regex("^sdelete"))
async def user_settings_delete(_, query):
  type = query.data.removeprefix("sdelete_")
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  if uts[user_id]['setting'].get(type, None) is not None:
    uts[user_id]['setting'][type] = None
    sync()
    
    await retry_on_flood(query.answer)("Successfully Deleted")
    await main_settings(_, query.message, query.from_user.id)
    
  else:
    await retry_on_flood(query.answer)(
      "You haven't set this yet.", show_alert=True
    )



@Bot.on_callback_query(filters.regex("^ufn"))
async def file_name_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)

  if query.data == "ufn_change":
    await retry_on_flood(
        query.edit_message_caption
    )("<b>📝 File name</b>\n<i>How each file is named.</i>\n\n<b>Placeholders</b>\n<code>{manga_title}</code> — manga name\n<code>{chapter_num}</code> — chapter number\n\n<i>Send the pattern now.</i>"
      )
    try:
      call = await _.listen(user_id=int(user_id), timeout=80, filters=filters.text)

      uts[user_id]['setting']["file_name"] = call.text
      sync()
      
      await igrone_error(call.delete)()
      await igrone_error(query.answer)(" Successfully Added ")

    except TimeoutError:
      await retry_on_flood(query.answer)("⏰ Timed out.")
    except Exception as err:
      await retry_on_flood(query.answer)(f"⚠️ {err}")

  elif query.data == "ufn_delete":
    if uts[user_id]['setting'].get("file_name", None) is not None:
      uts[user_id]['setting']["file_name"] = None
      sync()
      
      await igrone_error(query.answer)("Successfully Deleted")
    else:
      await retry_on_flood(query.answer)(
        "You haven't set this yet.", show_alert=True
      )

  elif query.data == "ufn_len_change":
    await retry_on_flood(
        query.edit_message_caption
    )("<b>📏 File name length</b>\n<i>Longest allowed file name.</i>\n\n<blockquote>Send a number — e.g. <code>30</code>.</blockquote>")

    try:
      call = await _.listen(user_id=int(user_id), timeout=80, filters=filters.text)
      try:
        len_ch = int(call.text)
        uts[user_id]['setting']["file_name_len"] = call.text
        sync()

        await igrone_error(call.delete)()
        await retry_on_flood(query.answer)("🤖 Successfully Added 🤖")

      except ValueError:
        await retry_on_flood(query.answer)(
          "That isn't a valid whole number."
        )

    except (TimeoutError):
      await retry_on_flood(query.answer)("⏰ Timed out.")
    except Exception as err:
      await retry_on_flood(query.answer)(f"⚠️ {err}")

  elif query.data == "ufn_len_delete":
    if uts[user_id]['setting'].get("file_name_len", None) is not None:
      uts[user_id]['setting']["file_name_len"] = None
      sync()

      await retry_on_flood(query.answer)("🤖 Successfully Deleted 🤖")
    else:
      await retry_on_flood(query.answer)(
        "You haven't set this yet.", show_alert=True
      )

  button = [
    [
      btn("✏️ Set / change", callback_data="ufn_change"),
      btn("🗑 Delete", callback_data="ufn_delete")
    ],
    [
      btn("✏️ Set length", callback_data="ufn_len_change"),
      btn("🗑 Remove length", callback_data="ufn_len_delete")
    ], 
    [
      btn("⇦ Back", callback_data="mus", style=None)
    ]
  ]
  
  await igrone_error(query.answer)()
  txt, thumbnali = get_user_txt(user_id)
  await retry_on_flood(query.edit_message_caption)(
    txt, reply_markup=InlineKeyboardMarkup(button)
  )

      

@Bot.on_callback_query(filters.regex("^uth"))
async def thumb_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)

  button = [
    [
      btn("✏️ Set / change", callback_data="uth_change"),
      btn("🔁 Use the manga's cover", callback_data="uth_constant")
    ],
    [
      btn("🗑 Delete", callback_data="uth_delete"),
      btn("⇦ Back", callback_data="mus", style=None),
    ]
  ]

  if query.data == "uth_constant":
    uts[user_id]['setting']["thumb"] = "constant"
    sync()
    
    await retry_on_flood(query.answer)("🎮 Successfully Added 🎮")

  elif query.data == "uth_change":
    await retry_on_flood(query.edit_message_caption)(
      "<b>📐 Send Thumbnail\n<u>Note:</u> <blockquote>You Can Send Links or Images Docs.. </blockquote></b>"
    )
    try:
      call = await _.listen(user_id=int(user_id), timeout=60)
      call_type = call.photo or call.document or None
      
      if call_type:
        call_type = call_type.file_id
        uts[user_id]['setting']["thumb"] = call_type
        sync()

      elif not call_type:
        call_type = call.text
        if call_type.startswith("http"):
          uts[user_id]['setting']["thumb"] = call_type
          sync()

        else:
          await retry_on_flood(query.answer)("That isn't a valid thumbnail.")

      await igrone_error(call.delete)()
      await retry_on_flood(query.answer)("🎮 Successfully Added 🎮")
      
    except TimeoutError:
      await retry_on_flood(query.answer)("⏰ Timed out.", show_alert=True)

    except Exception as err:
      await retry_on_flood(query.answer)(f"⚠️ {err}", show_alert=True)
      

  elif query.data == "uth_delete":
    if thumbnali:
      uts[user_id]['setting']["thumb"] = None
      sync()
    
    else:
      await retry_on_flood(query.answer)("You haven't set this yet.")
  
  txt, thumbnali = get_user_txt(user_id)
  txt += "\n\n<blockquote><b>ℹ️ Constant:</b> the manga's own poster will be used as the file's thumbnail.</blockquote>"
  if not thumbnali or thumbnali == "constant":
    thumbnali = random.choice(Vars.PICS)
  try:
    await retry_on_flood(query.edit_message_media)(
      media=InputMediaPhoto(thumbnali, txt),
      reply_markup=InlineKeyboardMarkup(button)
    )
  except:
    await retry_on_flood(query.edit_message_media)(
      media=InputMediaPhoto(random.choice(Vars.PICS), txt),
      reply_markup=InlineKeyboardMarkup(button)
    )
  
  await igrone_error(query.answer)()


@Bot.on_callback_query(filters.regex("^ubn"))
async def banner_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  banner1 = uts[user_id]['setting'].get("banner1", None)
  banner2 = uts[user_id]['setting'].get("banner2", None)
  banner_set = banner1 if banner1 else banner2
  
  if query.data.startswith("ubn_set"):
    banner_set = ""
    await retry_on_flood(query.edit_message_media)(
      InputMediaPhoto(
        random.choice(Vars.PICS),
        caption="<b>📐 Send Banner\n<u>Note:</u> <blockquote>You Can Send Links or Images Docs.. </blockquote></b>"
      )
    )

    try:
      call = await _.listen(user_id=int(user_id), timeout=60)
      call_type = call.photo or call.document or None

      if call_type:
        banner_set = call.photo.file_id
        
      elif not call_type:
        banner_set = call.text
        
        if not banner_set.startswith("http"):
          await retry_on_flood(query.answer)("📐 This Is Not Vaild Banner 📐")
          return

      if query.data == "ubn_set1":
        uts[user_id]['setting']["banner1"] = banner_set
        sync()

      elif query.data == "ubn_set2":
        uts[user_id]['setting']["banner2"] = banner_set
        sync()
      
      await retry_on_flood(call.delete)()
      await retry_on_flood(query.answer)("🎬 Successfully Added 🎬")

    except TimeoutError:
      await retry_on_flood(query.answer)("⏰ Timed out.", show_alert=True)

    except Exception as err:
      await retry_on_flood(query.answer)(f"⚠️ {err}", show_alert=True)

  elif query.data == "ubn_delete1":
    if banner1:
      uts[user_id]['setting']["banner1"] = None
      sync()
      
    else:
      return await retry_on_flood(query.answer)("You haven't set this yet.",
                                         show_alert=True)

  elif query.data == "ubn_delete2":
    if banner2:
      uts[user_id]['setting']["banner2"] = None
      sync()
    
    else:
      return await retry_on_flood(query.answer)("You haven't set this yet.",
                                         show_alert=True)
  
  if not banner_set:
    banner_set = random.choice(Vars.PICS)

  txt, _ = get_user_txt(user_id)
  button = [
    [
      btn("✏️ Set banner 1", callback_data="ubn_set1"),
      btn("🗑 Delete banner 1", callback_data="ubn_delete1")
    ],
    [
      btn("✏️ Set banner 2", callback_data="ubn_set2"),
      btn("🗑 Delete banner 2", callback_data="ubn_delete2")
    ], 
    [
      btn("⇦ Back", callback_data="mus", style=None)
    ]
  ]
  await igrone_error(query.answer)()
  try:
    await retry_on_flood(query.edit_message_media)(
      media=InputMediaPhoto(banner_set, txt),
      reply_markup=InlineKeyboardMarkup(button)
    )
  except:
    await retry_on_flood(query.edit_message_media)(
      media=InputMediaPhoto(random.choice(Vars.PICS), txt),
      reply_markup=InlineKeyboardMarkup(button)
    )




@Bot.on_callback_query(filters.regex("^u_file_type"))
async def type_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  if "type" not in uts[user_id]['setting']:
    uts[user_id]['setting']["type"] = []
    sync()

  type = uts[user_id].get("setting", {}).get("type", [])

  button = [[]]
  if "PDF" in type:
    button[0].append(
        btn("✅ PDF", callback_data="u_file_type_pdf"))
  else:
    button[0].append(
        btn("⬜ PDF", callback_data="u_file_type_pdf"))
  if "CBZ" in type:
    button[0].append(
        btn("✅ CBZ", callback_data="u_file_type_cbz"))
  else:
    button[0].append(
        btn("⬜ CBZ", callback_data="u_file_type_cbz"))

  button.append([btn("⇦ Back", callback_data="mus", style=None)])

  if query.data == "u_file_type_pdf":
    if "PDF" in type:
      uts[user_id]['setting']["type"].remove("PDF")
      sync()

      button[0][0] = btn("⬜ PDF",
                                          callback_data="u_file_type_pdf")

    else:
      uts[user_id]['setting']["type"].append("PDF")
      sync()

      button[0][0] = btn("✅ PDF",
                                          callback_data="u_file_type_pdf")

  elif query.data == "u_file_type_cbz":
    if "CBZ" in type:
      uts[user_id]['setting']["type"].remove("CBZ")
      sync()

      button[0][1] = btn("⬜ CBZ",
                                          callback_data="u_file_type_cbz")

    else:
      uts[user_id]['setting']["type"].append("CBZ")
      sync()

      button[0][1] = btn("✅ CBZ",
                                          callback_data="u_file_type_cbz")

  txt, _ = get_user_txt(user_id)
  await retry_on_flood(query.edit_message_media)(
    media=InputMediaPhoto(random.choice(Vars.PICS), txt),
    reply_markup=InlineKeyboardMarkup(button)
  )
  
  await igrone_error(query.answer)()





@Bot.on_callback_query(filters.regex("^uregex"))
async def regex_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  regex = uts[user_id]['setting'].get("regex", None)
  if query.data.startswith("uregex_set"):
    regex = query.data.split("_")[-1]
    uts[user_id]['setting'][f"regex"] = regex
    sync()
    
  elif query.data == "uregex_delete":
    if regex:
      regex = None
      uts[user_id]['setting']["regex"] = regex
      sync()
      
    else:
      await retry_on_flood(query.answer)("You haven't set this yet.")
  
  await igrone_error(query.answer)()
  button = [
    [
      btn(
        f"{'✅' if regex and str(regex) == str(i) else '' } {str(i)}", 
        callback_data=f"uregex_set_{i}") 
      for i in range(1, 5)
    ], 
    [
      btn("🗑 Delete", callback_data="uregex_delete"),
      btn("⇦ Back", callback_data="mus", style=None)
    ]
  ]
  
  txt, thumbnali = get_user_txt(user_id)
  await retry_on_flood(query.edit_message_media)(
    media=InputMediaPhoto(random.choice(Vars.PICS), txt),
    reply_markup=InlineKeyboardMarkup(button)
  )



@Bot.on_callback_query(filters.regex("^u_compress"))
async def compress_handler(_, query):
  user_id = str(query.from_user.id)
  ensure_user(user_id)
  
  compress = uts[user_id]['setting'].get("compress", None)
  
  def get_button(compress):
    compress = int(compress) if compress else 2
    button = []
    button = [
      btn(
        f"{'✅' if int(compress) == i else '' } {str(i)}",
        callback_data=f"u_compress_set_{i}"
      )
      for i in range(0, 105, 5)
    ]
    
    button = [button[x:x + 5] for x in range(0, len(button), 5)]
    
    button.append([
      btn("🗑 Delete", callback_data="u_compress_delete"),
      btn("⇦ Back", callback_data="mus", style=None)
    ])
    return InlineKeyboardMarkup(button)
  
  if query.data.startswith("u_compress_set"):
    compress = query.data.split("_")[-1]
    uts[user_id]['setting']["compress"] = compress
    sync()
    
    await retry_on_flood(query.answer)(" Successfully Added ")

  elif query.data == "u_compress_delete":
    if compress:
      uts[user_id]['setting']["compress"] = None
      sync()
      
      await retry_on_flood(query.answer)(" Successfully Deleted ")
    else:
      await retry_on_flood(query.answer)("You haven't set this yet.")
  
  txt, thumbnali = get_user_txt(user_id)
  await retry_on_flood(query.edit_message_media)(
    media=InputMediaPhoto(random.choice(Vars.PICS), txt),
    reply_markup=get_button(compress)
  )
