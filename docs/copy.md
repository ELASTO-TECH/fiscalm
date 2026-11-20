# Vocabularul fiscalm

Fiscalitate cu calm. Textul este simplu, calm și precis, ca explicația unui contabil
pentru un client. Aceste reguli se aplică aplicației, mesajelor motorului, instalării
și documentației care citează interfața.

Numele produsului se scrie întotdeauna **fiscalm**, cu litere mici, inclusiv la începutul propoziției, în titluri și în numele accesibile.

## Voce și formă

- Propoziții scurte, cu majusculă doar la început și la nume proprii. Diacritice românești.
- Adresare directă, la persoana a doua singular: „Alege”, „Încarcă”, „Verifică”. Nu forța pronumele „tu”.
- Fără ton publicitar, jargon, etichete decorative deasupra titlurilor, linie de pauză
  lungă sau punct median. Abrevierile fiscale necesare rămân: TVA, CUI, SPV, XML, IBAN.
- fiscalm nu gândește, nu încearcă și nu își amintește. Se numește operațiunea:
  „Se verifică extrasul”, „Regulă salvată”. O reîncercare tehnică se descrie ca reluare.
- Butonul numește acțiunea: „Salvează”, „Trimite în SPV”, „Am importat în bancă”. Mesajul
  rezultat repetă verbul: „Datele au fost salvate”. Nu promite un rezultat neconfirmat. fiscalm pregătește fișiere
  de plăți, nu plătește și nu trimite e-mailuri.
- Comutatorul numește funcția, iar explicația de sub el precizează efectul și limitele.
- Erorile spun ce nu s-a putut face și pasul următor. Confirmările spun efectul concret.
- Nu se traduc identificatori, câmpuri API, comenzi, nume de fișiere sau erori externe
  citate ca dovezi. Nu se schimbă comportamentul pentru a simplifica formularea.
- Verbe, nu substantive care ascund acțiunea („preluarea”, „includerea”, „confirmarea”); fără construcții
  pasive și fără „mediul de”.
- Nu personifica aplicația: facturile nu „pleacă”, aplicația nu „ține minte”. „Reîncearcă” rămâne
  potrivit pentru un buton acționat de utilizator.
- Denumirile Live și Sandbox nu se localizează.
- Ce se întâmplă fără o persoană se face „automat”, nu „singur”: „regula se aplică automat”.
- Etichetele nu au posesive și nu sunt întrebări: „Chei API”, nu „Cheile tale API”; „Plătitor”, nu „De la cine”.
- Româna obișnuită, nu calcuri: „manual”, nu „de mână”; „ordin de plată (OP)” la prima apariție.

| Înainte | După |
|---|---|
| Aici vezi ce ai de verificat sau de confirmat, din toate secțiunile. | (fără text) |
| Nicio factură pentru filtrele alese. | Niciun element |
| Includere automată în fișierul de plăți | Pune automat în fișierul de plăți |
| Preluarea extraselor de cont | Cum primești extrasele? |
| Mod de funcționare | Live sau Sandbox |
| Trimiterile de facturi folosesc doar mediul de test ANAF. | Facturile se trimit doar la ANAF Test. |
| Confirmă importul în bancă | Am importat în bancă |
| Închide detaliile | Închide |
| Pagina trebuie reîncărcată. | Reîncarcă pagina. |
| Leagă tranzacția / Tranzacții legate | Asociază tranzacția / Tranzacții asociate |
| De la cine / Către cine | Plătitor / Beneficiar |
| Rămas (lângă Încasat și Cheltuit) | Sold net |
| Din banii mei / Bani de drum, decont | Din fonduri proprii / Decont de cheltuieli |
| Toate cele 9 | Toate (9) |
| Rezultatul trimiterii este necunoscut. Factura trebuie verificată în SPV înainte de retrimitere. | Nu se știe dacă factura a ajuns în SPV. Verific-o în SPV înainte să o trimiți din nou. |

## Ecranul nu se explică

- Un ecran nu se descrie în proză. Nicio frază sub titlu care spune ce este pagina sau ce poți face pe ea;
  titlul, etichetele, starea și așezarea o spun. Fără „aici”, „de mai jos”, „poți” și fără fraze care repetă
  titlul sau anunță ce face un buton.
- Rămâne doar ce trebuie știut înainte de a acționa: o consecință, o limită, un fapt despre bani
  („fiscalm nu plătește”). Se scrie cât mai scurt, lângă controlul la care se referă, nu ca introducere.
- O listă goală spune „Niciun element”, la fel peste tot, cu filtre sau fără. Textul vine din componenta
  `Empty` (`EMPTY` în `ui.tsx`); o listă nu își scrie propriul mesaj.

## Termeni canonici

| Concept | Formulare și sens |
|---|---|
| Mod de lucru | **Live** și **Sandbox**, inclusiv în Setări și configurarea inițială |
| Live | Permite operațiunile externe autorizate; ANAF Producție. Nu înseamnă plată automată |
| Sandbox | Permite lucrul local și integrările de citire; trimiterile ANAF folosesc mediul de test |
| Company | firmă; „societate” numai în documente unde forma juridică o cere |
| Supplier / customer | furnizor / client; împreună: parteneri |
| Accountant | contabil; „dosar pentru contabil” pentru documentele pregătite |
| Draft | ciornă; nu este încă o factură emisă |
| Outgoing / incoming invoice | factură emisă / factură primită |
| Issue / invoice number | emite factura / serie și număr; data emiterii |
| Due date / overdue | scadență, scadentă azi, restantă; restanța presupune depășirea scadenței |
| Invoice payable / receivable | de plătit / de încasat |
| Payment / collection | plată / încasare; factură primită plătită / factură emisă încasată |
| Human marking without proof | în curs de confirmare; marcarea nu confirmă plata sau încasarea |
| Partial payment | plată parțială / încasare parțială; rest de plată / rest de încasat |
| Credit note | factură de stornare; acțiune: stornează factura |
| e-Factura | RO e-Factura pentru sistemul național; e-Factura în etichetele scurte |
| SPV | Spațiul Privat Virtual (SPV); nu este numele instituției care validează |
| Upload | trimite în SPV / trimisă la ANAF; index de încărcare pentru identificatorul ANAF |
| Upload pending | se trimite / trimisă, se așteaptă răspunsul ANAF |
| Validation result | acceptată de ANAF / respinsă de ANAF; nu implică încasarea sau acceptarea comercială |
| Unknown upload result | rezultat necunoscut; verificare în SPV înainte de retrimitere |
| Test response | se precizează mediul de test ANAF și lipsa valorii fiscale |
| Bank statement | extras de cont; tranzacție bancară, debit / credit numai unde sunt necesare |
| Bank account / card | cont bancar / card; titularul cardului (nu „proprietarul cardului”) |
| Payment file | fișier de plăți; se descarcă și se importă de o persoană în aplicația băncii |
| Automatic preparation | pune automat în fișierul de plăți; nu „plată automată” |
| Receipt | bon fiscal / chitanță, după tipul documentului; nu sunt sinonime |
| Settlement | decont pentru document; decontare pentru operațiune; comision pentru reținerea aferentă |
| Cash on delivery | ramburs; raport de decontare și încasări ramburs |
| Reconciliation | asociere cu tranzacția (verbul „asociază”, nu „leagă”); confirmarea plății numai pe baza dovezii |
| Counterparty on bank statement | plătitor / beneficiar (nu „de la cine / către cine” sau „IBAN-ul celuilalt”) |
| Cash flow balance | sold net (diferența încasat minus cheltuit pe perioadă, nu „Rămas”); solduri bancare pentru soldul conturilor |
| Paid from personal funds | din fonduri proprii, cu explicația „Decont de cheltuieli” (nu „din banii mei” sau „bani de drum”) |
| Invoice totals / dates | total fără TVA, data emiterii, scadență, previzualizare PDF |
| Rule / suggestion | regulă salvată / propunere; propunerea nu se aplică automat |
| Mail | e-mail; acces doar pentru citire |
| History import | import de facturi anterioare; nu emitere și nu trimitere la ANAF |
| Authentication | autentificare; parolă, cheie de acces (passkey), cod de recuperare |
| API key | cheie API; pagina Linkuri și chei API, alături de linkurile Bondrop; nu „acces” |
| Integration | integrare pentru serviciu; conexiune pentru legătura autorizată |
| Backup | copie de siguranță; restaurare |

## Stări

Un singur nume pentru fiecare stare, același în liste, în fișe și în Plăți (`app/src/components/derive.ts`). Motorul
decide starea; aplicația doar o numește. Verde numai pentru ce dovedește o probă: un extras, un e-mail de plată sau
bonul fiscal pentru plăți, extrasul sau raportul de decontare pentru încasări.

| Ce | Stare (motor) | Formulare |
|---|---|---|
| Factură primită | deschisă | **De plătit**, cu scadența: „De plătit, scadentă pe 6 noiembrie”, „De plătit, restantă de 3 zile” |
| | în fișier, înainte de descărcare | **În fișierul <nume>**; „Lipsește extrasul din octombrie 2026” când motorul reține o plată automată |
| | în fișier | **În fișierul de plăți**; „import neconfirmat” când fișierul lipsește din dosar |
| | importată | **Importată în bancă, se așteaptă extrasul** |
| | marcată de o persoană, sau plătită fără dovadă | **În curs de confirmare**, cu data; niciodată „Plătită” |
| | marcată de o persoană care a spus cum a plătit | **Marcată ca plătită**, cu felul plății: „Marcată ca plătită, cu cardul”; în fișă, rândul despre dovada care lipsește |
| | plătită și dovedită | **Plătită**, cu data, în verde |
| | nu se mai plătește | **Nu se mai plătește**; **Stornată** când o factură de stornare o acoperă |
| | factură de stornare fără factură găsită | **Factură de stornare, neasociată** |
| Factură emisă | deschisă | **De încasat**, cu scadența |
| | marcată de o persoană | **În curs de confirmare**, cu data |
| | încasată | **Încasată**, cu data, în verde |
| | stornată integral | **Stornată** |
| | factura de stornare | **Factură de stornare pentru** seria și numărul facturii stornate |
| Factură din istoric | importată din alt program, fără plăți urmărite | **Din istoric**, cu data; fără stare de plată, niciodată „De plătit” sau „De încasat” |
| Bon sau chitanță | fără factură în aceeași cheltuială | **Fără factură** |
| | cu factură | **Cu factura** seria și numărul |
| Dosar pentru contabil | extrasul lunii lipsește | **În lucru** |
| | se poate pregăti | **De pregătit** |
| | pregătit, netrimis | **De trimis** |
| | trimis | **Trimis**, în verde |

Textele propuse se prezintă în PR și primesc review lingvistic la nivel nativ.

Termenii fiscali urmează [normele privind facturarea, în aplicarea art. 319 din Codul fiscal](https://static.anaf.ro/static/10/Anaf/legislatie/HG_1_2016_norme%20CF.pdf).
Trimiterea, verificarea structurii și comunicarea facturii sunt operațiuni distincte în
[OUG 120/2021 privind RO e-Factura](https://legislatie.just.ro/Public/DetaliiDocument/305507).
Pentru transmitere și validare se consultă [ghidul ANAF RO e-Factura](https://static.anaf.ro/static/10/Anaf/AsistentaContribuabili_r/Ghid_RO_eFactura.pdf).
Pentru pașii SPV se consultă [întrebările și răspunsurile Ministerului Finanțelor](https://mfinante.gov.ro/documents/4398723/5002737/E-facturaFAQ.pdf).
„Acceptată de ANAF” este eticheta scurtă a rezultatului tehnic de validare, nu o opinie
fiscală asupra conținutului și nici dovada plății.
