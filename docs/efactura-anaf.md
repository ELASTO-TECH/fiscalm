# Conectează ANAF e-Factura

Ghid pentru cine instalează fiscalm. Emiterea facturilor și regulile ei sunt în [invoicing.md](invoicing.md)
(în engleză, pentru cine contribuie la cod).

## 1. Ce face

fiscalm citește direct din API-ul ANAF facturile pe care firmele instalării le **primesc** în SPV și pe cele pe
care le **emit**, și trimite în SPV facturile emise în fiscalm. Pentru asta, ANAF cere o aplicație OAuth
înregistrată de cine administrează instalarea și o autorizare făcută o dată pe an cu certificatul digital
calificat al unei persoane cu drept în SPV pentru firme. Totul se face din browser, fără linia de comandă.

Poți adăuga mai multe conexiuni ANAF, fiecare autorizată cu certificatul persoanei care are drepturile necesare.
La adăugare, toate firmele sunt bifate. Opțiunea „Toate firmele” include și firmele adăugate ulterior;
o listă explicită acoperă doar firmele alese. Poți modifica alegerea din Integrări.

Pentru fiecare firmă, fiscalm folosește conexiunea care a citit-o ultima dată cu succes, dacă încă o acoperă;
altfel, pe cea mai veche dintre conexiunile active care o acoperă. Dacă sunt mai multe, apare o avertizare
pentru acea firmă. O eroare nu mută citirea pe alt certificat. O firmă fără conexiune apare ca neconectată;
celelalte firme continuă să fie citite. Autorizarea fiecărei conexiuni se face separat.

## 2. Adresa publică

fiscalm trebuie să fie accesibil pe https, la o adresă publică (de exemplu `https://fiscalm.exemplu.ro`).
Adresa se setează o dată, la instalare: `FISCALM_PUBLIC_URL=https://…` în `.env`-ul folosit de Docker Compose. Din ea, fiscalm face
adresa la care ANAF trimite browserul înapoi: `<adresa publică>/anaf/callback`. ANAF acceptă doar adrese https.

## 3. Cont de dezvoltator

O singură dată:

1. anaf.ro → **Servicii Online** → **Înregistrare utilizatori** → **DEZVOLTATORI APLICAȚII** →
   **Înregistrare pentru API-uri** (formularul de la `https://www.anaf.ro/InregOauth`).
2. Completează formularul; codul de verificare vine pe e-mail.
3. **Autentificare utilizator** → **Editare profil Oauth**.

## 4. Aplicația OAuth

În **Gestionare aplicații**:

| Câmp | Ce se trece |
|---|---|
| Denumire aplicație | de exemplu `fiscalm` |
| Serviciu | **E-Factura** |
| Callback URL | adresa din fiscalm, Setări › Integrări › ANAF e-Factura, „Adresa de redirecționare (Callback URL)”, exact așa |

Apasă **Generare Client ID**. În aplicația creată apar **Client ID** și **Client Secret**.

## 5. Integrarea în fiscalm

În Setări › Integrări › ANAF e-Factura, adaugă integrarea cu Client ID și Client secret. Secretul rămâne pe
server și nu se mai afișează. Nu există un selector de mediu: trimiterile urmează modul instalării (secțiunea 7).

Schimbarea Client ID-ului (altă aplicație OAuth) cere o conectare nouă; schimbarea doar a secretului, nu.
Ștergerea integrării șterge și tokenurile. Revocarea la ANAF se face din portalul ANAF.

## 6. Conectează ANAF

Durează cam două minute și se face o dată pe an. Ai nevoie de cel puțin o firmă în Setări › Firme.

Un administrator deschide integrarea și apasă **Conectează ANAF** în browserul care are certificatul digital.
fiscalm deschide pagina ANAF (logincert.anaf.ro), care cere certificatul și PIN-ul. ANAF trimite apoi browserul
înapoi la fiscalm, care arată „ANAF este conectat” și data până la care ține conexiunea. Fă pașii dintr-o
bucată: codul dat de ANAF expiră într-un minut, iar legătura pornită din fiscalm în zece minute.

Dacă ANAF refuză, fiscalm te aduce înapoi la integrare și arată motivul lângă butonul **Conectează ANAF**, timp de
zece minute sau până la următoarea încercare. Un browser în care ai închis fereastra de alegere a certificatului
nu o mai arată până îl repornești: închide-l complet, deschide-l din nou și conectează ANAF.

Certificatul trebuie să aibă drept în SPV (reprezentant legal sau desemnat) pentru firme. fiscalm salvează
conexiunea numai dacă certificatul poate citi cel puțin una dintre firmele instalării; altfel păstrează
conexiunea veche și arată de ce. Dacă pentru o firmă lipsește dreptul, fiscalm arată avertismentul ANAF.

După conectare:

- Tokenul de acces ține 90 de zile și se reînnoiește singur, cu 14 zile înainte să expire.
- Autorizarea ține **365 de zile**. Cu 30 de zile înainte, integrarea și Acasă arată „Expiră curând”; apasă
  **Reconectează ANAF**.
- Tokenurile nu apar în aplicație, în jurnale sau în `fiscalm efactura auth-status`, care arată doar datele de expirare.

## 7. Sandbox și ANAF Test

ANAF are un mediu de test și unul de producție. fiscalm le alege după modul instalării:

| | Sandbox | Live |
|---|---|---|
| Citirea facturilor primite și emise | Producție | Producție |
| Trimiterea manuală în SPV și verificarea răspunsului | ANAF Test | Producție |
| Trimiterea automată, după zilele alese în integrare (implicit 4) | nu | da, dacă firma o are pornită |

Conectarea merge și în Sandbox: face doar citiri. O factură emisă în Sandbox nu se trimite automat, nici după
trecerea în Live; o trimiți manual. Stările sunt separate pe mediu: o factură acceptată în ANAF Test nu are
valoare fiscală, iar după trecerea în Live apare ca netrimisă în producție. Răspunsurile din ANAF Test se
păstrează, dar nu devin o a doua factură emisă. Factura se poate încasa de la emitere, în Sandbox ca și în Live,
fie că a trimis-o cineva în SPV, fie că nu.

Atenție: ANAF Test a acceptat facturi trimise din fiscalm (10 noiembrie 2026) către o firmă din România, o
persoană fizică și o firmă din UE, o factură de stornare și una în valută. ANAF a respins o factură modificată
în timpul trimiterii, iar fiscalm a înregistrat respingerea. ANAF a acceptat apoi factura trimisă din nou, fără
modificări. Facturile emise de neplătitori de TVA nu au fost verificate cu ANAF, iar în producție nu s-a trimis
nimic. Verifică în SPV primele facturi pe care le trimiți.

Trecerea între Sandbox și Live nu cere reconectarea ANAF: după procedura ANAF, același token servește Test și
Producție. ANAF Test a acceptat un token obținut prin autorizarea obișnuită; conectarea din fiscalm
(„Conectează ANAF”) și producția nu au fost încercate cu ANAF. Schimbarea aplicației OAuth cere o conectare nouă.

`FISCALM_ANAF_ENV` este doar pentru dezvoltare și teste; pe o instalare lipsește.

## 8. Facturi emise și încasări

Facturile emise nu cer altă autorizare: vin din același serviciu E-Factura, cu același token. La fiecare
rulare, fiscalm citește facturile primite și emise în ultimele 60 de zile, păstrează ZIP-ul original de la ANAF
(cele emise în `<FISCALM_HOME>/efactura/<firmă>/emise/`) și le arată cu scadența. O factură emisă în fiscalm
apare în listă de la emitere; copia ei citită din SPV doar confirmă trimiterea și nu se adaugă a doua oară. Apoi:

- o factură emisă devine încasată singură când într-un extras de cont (încărcat, primit pe e-mail sau citit
  prin open banking) apare o încasare cu suma rămasă de plată, de la acel client: CUI-ul lui în detalii,
  numele lui exact sau IBAN-ul de pe care a mai plătit. O încasare închide o singură factură; dacă se
  potrivește cu mai multe, nu închide niciuna;
- facturile de stornare scad suma datorată de factura la care se referă;
- încasările în numerar sau prin compensare le marchezi tu, cu „Marchează ca încasată”; marcarea se poate
  anula. O încasare se poate repartiza explicit mai multor facturi din fișa tranzacției.

Proformele și facturile furnizorilor străini nu sunt în e-Factura și vin din e-mail
([facturi din e-mail](mail-invoices.md)).

### Erori și avertismente ANAF

Facturile primite și cele emise se citesc separat. Pentru fiecare dintre ele, fiscalm ține minte, pe fiecare firmă,
dacă a citit-o cu succes măcar o dată (inclusiv o listă goală). O eroare apare pe Acasă („Facturile din ANAF nu au
putut fi preluate”); erorile și avertismentele apar în `fiscalm status` și în starea din API (`system.anaf`):

- ANAF refuză accesul (HTTP 403) pentru o firmă citită înainte: **eroare**. Pentru o firmă niciodată citită:
  **avertisment**, de obicei pentru că dreptul în SPV lipsește încă. Celelalte firme se citesc în continuare.
- ANAF refuză accesul pentru **toate** firmele instalării: **eroare**, chiar și la o instalare nouă. Nu e un
  avertisment obișnuit de început: verifică certificatul și dreptul lui în SPV, apoi reconectează ANAF.
- Lipsa tokenului sau un token expirat, limita de cereri ANAF și erorile de rețea sunt erori.
- O factură care nu s-a putut descărca de trei ori într-o zi se reia a doua zi (ANAF permite zece descărcări pe zi
  pentru un mesaj, așa că rămân încercări pentru tine).
- Avertismentele se înlocuiesc după fiecare citire completă a firmelor. O problemă cu tokenul oprește citirea
  înainte de acest pas, așa că avertismentele vechi rămân. Ultima eroare rămâne afișată până la o citire completă
  fără erori și fără avertismente; un avertisment nu o șterge.

Căutarea unei firme după CUI folosește serviciul public ANAF, fără autentificare, și în Sandbox.

## 9. Limitări cunoscute

- Trimiterea a fost verificată doar cu ANAF Test (secțiunea 7). În producție nu a fost trimisă nicio factură.
- Validarea locală a XML-ului nu înseamnă că SPV acceptă factura. Pentru firme străine, validatorul public
  ANAF răspunde `ERRIdentif`.
- Nu există încă un avertisment pentru termenul de cinci zile lucrătoare de trimitere.
- ANAF listează cel mult 60 de zile în urmă: facturile mai vechi se aduc prin
  [importul de facturi anterioare](history-import.md).
