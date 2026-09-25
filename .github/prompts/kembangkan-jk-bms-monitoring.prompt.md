---
name: "Kembangkan JK-BMS Monitoring"
description: "Rancang dan implementasikan satu fitur baru atau perbaikan pada monitoring JK-BMS melalui Bluetooth LE dan ekspor SNMP."
argument-hint: "Jelaskan fitur atau perubahan yang ingin dibuat"
agent: "agent"
---

Kita sedang mengembangkan sistem monitoring JK-BMS melalui Bluetooth LE dan menyediakan datanya melalui SNMP. Kerjakan satu permintaan perubahan berikut:

**Permintaan:** ${input:feature:jelaskan fitur atau perbaikan yang diinginkan}

Gunakan konteks workspace dan file terkait, terutama:
- [jk_bms_monitor.py](../../jk_bms_monitor.py)
- [jk_bms_passpersist.py](../../jk_bms_passpersist.py)
- [jk_bms_snmp.py](../../jk_bms_snmp.py)

Ikuti alur kerja ini:

1. Telusuri implementasi lokal yang paling relevan sebelum mengubah kode. Identifikasi sumber data BLE, parser frame, cache `BMSData`, jalur SNMP, dan pola pengujian atau validasi yang sudah tersedia.
2. Nyatakan hipotesis singkat tentang lokasi kontrol perilaku dan satu pemeriksaan murah yang dapat membuktikannya salah.
3. Implementasikan perubahan terkecil yang menyelesaikan permintaan, dengan mempertahankan gaya, API, dan kompatibilitas yang sudah ada.
4. Jika menambah metrik, tentukan satuan, skala SNMP, tipe nilai, dan OID secara konsisten. Jangan mengubah OID lama tanpa alasan kompatibilitas yang jelas.
5. Untuk `pass_persist`, jangan menulis diagnostik ke stdout karena stdout adalah protokol SNMP; gunakan stderr untuk log.
6. Tangani frame BLE yang terfragmentasi, checksum, koneksi ulang, data stale, nilai kosong, serta perangkat atau frame yang tidak lengkap tanpa membuat worker berhenti.
7. Tambahkan atau perbarui pemeriksaan yang paling dekat dengan perubahan. Hindari refactor dan perubahan file yang tidak diperlukan.
8. Jalankan validasi terfokus yang tersedia, lalu laporkan hasilnya. Bila validasi tidak dapat dijalankan, jelaskan alasannya dan berikan perintah yang dapat dijalankan pengguna.

Format jawaban:
- Ringkas perubahan dan alasan teknisnya.
- Sebutkan file yang diubah sebagai tautan workspace.
- Laporkan validasi yang dijalankan dan hasilnya.
- Catat asumsi, batasan protokol JK-BMS, atau keputusan OID yang perlu dikonfirmasi.
