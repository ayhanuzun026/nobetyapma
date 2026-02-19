"""
Excel export fonksiyonu — greedy_solver'a bağlı.
"""

from datetime import date
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


def create_excel(yil, ay, yonetici):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Nöbet Listesi"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    weekend_fill = PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid")
    center = Alignment(horizontal='center', vertical='center')
    border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    headers = ["Tarih", "Gün"]
    for g in yonetici.gorevler:
        headers.append(g.ad)
    ws.append(headers)

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    tr_gunler = {
        "Pazar": "Paz", "Cumartesi": "Cmt", "Cuma": "Cum",
        "Persembe": "Prş", "Pazartesi": "Pzt", "Sali": "Sal", "Carsamba": "Çar"
    }

    for gun in range(1, yonetici.days_in_month + 1):
        dt = date(yil, ay, gun)
        gun_adi_long = yonetici.takvim[gun]
        gun_kisa = tr_gunler.get(gun_adi_long, gun_adi_long)

        row_data = [dt.strftime("%d.%m.%Y"), gun_kisa]
        atamalar = yonetici.cizelge[gun]
        for kisi in atamalar:
            row_data.append(kisi if kisi else "-")
        ws.append(row_data)

        if gun_adi_long in ["Cumartesi", "Pazar"]:
            for cell in ws[ws.max_row]:
                cell.fill = weekend_fill

    # İstatistik sayfası
    ws_stat = wb.create_sheet("İstatistik")
    ws_stat.append(["Personel", "Hedef", "Gerçekleşen", "Fark", "Kalan H.İçi", "Kalan Pzr", "Mazeret Gün"])

    for p in yonetici.personeller:
        gerceklesen = len(p.atanan_gunler)
        fark = gerceklesen - p.hedef_toplam
        ws_stat.append([p.ad, p.hedef_toplam, gerceklesen, fark, p.kalan_hici, p.kalan_pzr, p.mazeret_sayisi])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
