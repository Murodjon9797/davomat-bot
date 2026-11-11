# O'qituvchilar davomati (QR kod + lokatsiya + Telegram Mini App)

## Qanday ishlaydi
1. Admin texnikum lokatsiyasini kiritadi va QR kodni chop etib, o'qituvchilar xonasi yoki kirish joyiga osadi.
2. **O'qituvchi** darsga kirganda ilovadagi markaziy tugma bilan QR ni skanerlaydi → telefon joylashuvi olinadi → server texnikum hududida ekanini tekshiradi → guruh va fanni tanlab «Darsga kirdim» bosadi.
3. Darsdan chiqqanda yana skanerlaydi → chiqish vaqti va dars davomiyligi yoziladi.
4. Chiqish belgilanmasa, dars belgilangan vaqtdan keyin (standart 4 soat) «chiqish belgilanmagan» deb yopiladi va o'qituvchiga xabar boradi.

Oddiy telefon kamerasi bilan skanerlansa ham ishlaydi: bot ochiladi va «📍 Tasdiqlash» tugmasi joylashuvni tekshiradi.

## Admin panel (ilovadagi 🛡 tugma)
- **Statistika** — hozir darsdagilar (jonli), bugungi darslar va soatlar, 7 kunlik grafik, oylik reyting, bugun kelmaganlar, Excel hisobot (umumiy + batafsil).
- **Jurnal** — sana, o'qituvchi, guruh, holat bo'yicha filtr; darsni hozir yopish, chiqish vaqtini tuzatish, o'chirish.
- **O'qituvchilar** — qidiruv, oylik soatlar, tarix, ismni tahrirlash, admin qilish, o'chirish.
- **Guruhlar** — o'qituvchi darsga kirishda tanlaydigan guruhlar.
- **Xabar** — hammaga, bugun dars belgilamaganlarga yoki profilini to'ldirmaganlarga.
- **Sozlamalar** — texnikum lokatsiyasi (xarita, «Hozirgi joyimni olish», radius), lokatsiya tekshiruvini yoqish/o'chirish, muassasa nomi, dars yopilish vaqti, QR kodni chop etish uchun rasm qilib olish va yangilash.

Birinchi marta botga `/start` bosgan odam avtomatik admin bo'ladi (yoki `ADMIN_IDS`).
O'z ID ingizni bilish: `/id`.

## Ishga tushirish
```bash
pip install -r requirements.txt
python bot.py
```
Mini ilova uchun HTTPS manzil kerak (`WEBAPP_URL`). Render'da `render.yaml` orqali avtomatik sozlanadi (webhook rejimi).

## Fayllar
- `bot.py` — bot + API server + SQLite baza (`davomat.db`)
- `webapp/index.html` — mini ilova (dizaynni brauzerda ko'rish: `?demo=1`)
- `.env` — sozlamalar (token shu yerda, hech kimga bermang)
