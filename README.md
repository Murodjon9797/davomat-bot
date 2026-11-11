# Davomat bot (QR kod + Mini App)

## Qanday ishlaydi
1. **Admin** ilovada dars yaratadi → ekranda QR kod chiqadi (har 10 soniyada yangilanadi, 45 soniyadan keyin eskiradi — rasmga olib yuborib bo'lmaydi).
2. **Talaba** ilovadagi markaziy tugma bilan QR ni skanerlaydi (yoki oddiy telefon kamerasi bilan — bot ochilib, davomat avtomatik belgilanadi).
3. Admin ekranida kelganlar ro'yxati jonli yangilanadi, «CSV» tugmasi Excel'da ochiladigan hisobotni botga yuboradi.

Birinchi marta botga `/start` bosgan odam avtomatik admin bo'ladi (yoki `.env` dagi `ADMIN_IDS`).
Boshqa admin qo'shish: `/addadmin 123456789`. O'z ID ingizni bilish: `/id`.

## Ishga tushirish
```bash
pip install -r requirements.txt
```

Mini ilova uchun **HTTPS manzil** kerak. Kompyuterda sinash uchun Cloudflare tunnel:
```bash
cloudflared tunnel --url http://localhost:8080
```
Chiqqan `https://....trycloudflare.com` manzilini `.env` dagi `WEBAPP_URL=` ga yozing, keyin:
```bash
python bot.py
```

Doimiy ishlashi uchun serverga (VPS, Railway, Render va h.k.) joylang va `WEBAPP_URL` ni o'sha domenga qo'ying.

## Fayllar
- `bot.py` — bot + API server + SQLite baza (`davomat.db`)
- `webapp/index.html` — mini ilova (dizaynni brauzerda ko'rish: `?demo=1`)
- `.env` — sozlamalar (token shu yerda, hech kimga bermang)
