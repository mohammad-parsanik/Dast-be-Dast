# Bank SMS samples

Format references for the SMS parsers (PLAN.md §5.3). **Amounts, balances and account digits are
anonymised**; only the layout matches the real messages. Amounts are in rials.

## Pasargad (پاسارگاد) — credit
```
201.8000.2xxxxxx9.1
+12,000,000
07/04_17:41
مانده: 150,300,000
```

## Melli (ملی) — Paya credit
```
بانك ملي ايران
پايا:80,000,000+
حساب:82xxx
مانده:95,000,000
0701-19:18
```

## Resalat (رسالت) — credit
```
10.9xxxx11.1
+30,000,000
07/14_09:18
مانده: 41,000,000
```

## Mehr Iran (مهر ایران) — group payment, no sign
```
پرداخت گروهي
حساب: 300xxxxxx000000001
مبلغ:25,000,000
مانده:33,000,000
زمان:1405/6/23-10:07
```

Notes: Arabic ي/ك appear in Melli and Mehr messages; trailing spaces occur; year is missing except Mehr.
