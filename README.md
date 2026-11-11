# O'qituvchilar davomati (QR kod + lokatsiya + Telegram Mini App)

## Qanday ishlaydi
1. Admin texnikum lokatsiyasini kiritadi va QR kodni chop etib, o'qituvchilar xonasi yoki kirish joyiga osadi.
2. **O'qituvchi** darsga kirganda ilovadagi markaziy tugma bilan QR ni skanerlaydi → telefon joylashuvi olinadi → server texnikum hududida ekanini tekshiradi → guruh va fanni tanlab «Darsga kirdim» bosadi.
3. Darsdan chiqqanda yana skanerlaydi → chiqish vaqti va dars davomiyligi yoziladi.
4. Chiqish belgilanmasa, dars belgilangan vaqtdan keyin (standart 4 soat) «chiqish belgilanmagan» deb yopiladi va o'qituvchiga xabar boradi.

Oddiy telefon kamerasi bilan skanerlansa ham ishlaydi: bot ochiladi va «📍 Tasdiqlash» tugmasi joylashuvni tekshiradi.

## Kirish kodi (faqat ruxsat berilganlar)
Botga yangi odam /start bosganda «admin bergan kirish kodini yuboring» degan javob oladi — kodsiz bot ham, ilova ham ishlamaydi.
- **🔑 Kodlar** bo'limida kod yaratiladi: o'qituvchi uchun (1 / 5 / 20 / 50 / 200 kishilik, muddati bilan) yoki admin uchun.
- «📩 Botga yuborish» — taklif xabari (havola + kod) botga keladi, uni o'qituvchiga uzatasiz. Havolani bosgan odam avtomatik ro'yxatdan o'tadi.
- Kodsiz /start bosganlar «Kod kutayotganlar» ro'yxatida chiqadi — ✅ bilan ruxsat berish yoki ✕ bilan rad etish mumkin.
- **Bosh admin** (botga birinchi kirgan odam yoki `ADMIN_IDS`) yagona bo'lib admin qo'sha/olib tashlay oladi. Boshqa adminlar faqat o'qituvchi kodlarini yaratadi.

## Admin panel (ilovadagi 🛡 tugma)
- **Statistika** — hozir darsdagilar (jonli), bugungi darslar va soatlar, 7 kunlik grafik, oylik reyting, bugun kelmaganlar, Excel hisobot (umumiy + batafsil).
- **Jurnal** — sana, o'qituvchi, guruh, holat bo'yicha filtr; darsni hozir yopish, chiqish vaqtini tuzatish, o'chirish.
- **O'qituvchilar** — qidiruv, oylik soatlar, tarix, ismni tahrirlash, admin qilish, o'chirish.
- **Guruhlar** — o'qituvchi darsga kirishda tanlaydigan guruhlar.
- **Xabar** — hammaga, bugun dars belgilamaganlarga yoki profilini to'ldirmaganlarga.
- **Sozlamalar** — texnikum lokatsiyasi (xarita, «Hozirgi joyimni olish», radius), lokatsiya tekshiruvini yoqish/o'chirish, muassasa nomi, dars yopilish vaqti, QR kodni chop etish uchun rasm qilib olish va yangilash.

## Kompyuterda (Telegram Desktop)
Mini ilova Telegram Desktop'da ham ochiladi va keng oynaga moslashadi: o'qituvchi sahifasi ikki ustunli,
admin panel keng jadvallar bilan. Admin uchun yuqorida «📺 Ekran rejimi» (katta QR, soat, «hozir darsda»,
so'nggi harakatlar — kirish joyidagi monitor uchun) va «⛶ To'liq ekran» tugmalari bor.
Darsga kirish/chiqishni skanerlash telefonda qilinadi (kamera va GPS kerak).

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
