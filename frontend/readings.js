/* Was jolt aus dem Fahrzeug liest - als Tabelle, nicht als Programm.
 *
 * Vorher stand jede Umrechnung als eigene JavaScript-Funktion in
 * `obd-core.js`. Wer eine Datenkennung ergaenzen wollte, musste dort Code
 * schreiben; wer eine Byte-Lage berichtigen wollte, musste ihn lesen. Hier
 * ist beides eine Zeile mit benannten Feldern - und die Felder sind genau
 * die Groessen, die in den MEB-Referenzen ohnehin danebenstehen.
 *
 * Eine JS-Datei und keine .json: Es ist dieselbe Tabelle, nur mit
 * Kommentaren. Und die tragen hier das, was sonst verloren ginge - woher
 * eine Formel stammt, welche am Fahrzeug bestaetigt ist und welche geraten
 * war. Ausserdem laedt sie ueber ein Skript-Tag, ohne dass `obd-core.js`
 * auf ein `fetch` warten muesste, das unterwegs ins Funkloch laeuft.
 *
 * ===================================================================
 * Wie eine Zeile gelesen wird
 * ===================================================================
 *
 * Aus der Antwort des Steuergeraets werden die Nutzbytes geschnitten - das
 * ist alles nach der Quittung (`62` + die zwei Bytes der Datenkennung).
 * `b[0]` ist also das erste Byte **nach** der Quittung. Daraus:
 *
 *     roh   = die Bytes `ab` bis `ab + laenge - 1`, hoechstwertiges zuerst
 *     roh  &= maske                       (falls angegeben)
 *     wert  = (roh + vorversatz) / teiler * faktor + versatz
 *     wert  = |wert|                      (falls betrag)
 *     wert  = null, wenn wert < min oder wert > max
 *
 * Reichen die Bytes nicht bis `ab + laenge`, kommt null - die Zeile bleibt
 * dann leer, statt eine Zahl zu erfinden.
 *
 * ===================================================================
 * Die Felder
 * ===================================================================
 *
 * Pflicht je Eintrag:
 *
 *   name       Schluessel in den Rohwerten, z.B. "soc_roh". Aendern heisst
 *              die Aufzeichnung aendern - alte Fahrten kennen den alten
 *              Namen.
 *   titel      Wie es in der Oberflaeche heisst.
 *   did        Datenkennung, wie sie hinausgeht: "22" + Parameter,
 *              z.B. "22028C".
 *   adresse    Name eines Eintrags aus `adressen` weiter unten.
 *   ab         Erstes Nutzbyte, 0-basiert.
 *   laenge     Anzahl Bytes, hoechstwertiges zuerst.
 *
 * Freiwillig, mit Vorgabe:
 *
 *   einheit    null heisst: ohne Einheit.            (Vorgabe null)
 *   stellen    Nachkommastellen in der Anzeige.      (Vorgabe 1)
 *   teiler     Durch diese Zahl teilen.              (Vorgabe 1)
 *   faktor     Damit malnehmen.                      (Vorgabe 1)
 *   versatz    Danach dazuzaehlen, z.B. -40 bei °C.  (Vorgabe 0)
 *   vorversatz Vor dem Teilen dazuzaehlen.           (Vorgabe 0)
 *   vorzeichen true: Zweierkomplement statt vorzeichenlos. (Vorgabe false)
 *   betrag     true: Betrag nehmen.                  (Vorgabe false)
 *   maske      Bit-Maske auf den Rohwert, z.B. 1.    (Vorgabe: keine)
 *   min, max   Plausibilitaetsgrenzen; ausserhalb gilt der Wert als
 *              unbrauchbar und wird null.
 *   pflicht    true: Ohne diesen Wert wird die ganze Runde verworfen.
 *              Genau ein Eintrag hat das - der Ladestand. (Vorgabe false)
 *   selten     Nur jede n-te Runde abfragen. 0 heisst jede. (Vorgabe 0)
 *   auch       Weitere Werte aus **derselben** Antwort, als Liste. Jeder
 *              Eintrag darin kennt dieselben Felder ausser `did`,
 *              `adresse`, `pflicht` und `selten` - die kommen vom
 *              Hauptwert, weil es dieselbe Abfrage ist.
 *
 * `vorversatz` und `versatz` sind zwei Felder und nicht eines, weil beide
 * Reihenfolgen wirklich vorkommen: Der Batteriestrom ist
 * `(roh - 150000) / 100`, die Batterietemperatur `roh / 2 - 40`. Wer das
 * verwechselt, bekommt Zahlen, die plausibel aussehen und falsch sind.
 */
window.joltReadings = {

  /* ===================================================================
   * Zieladressen
   * ===================================================================
   *
   * Eine Adresse besteht aus drei Teilen, und der dritte wird gern
   * vergessen: `cp` sind die oberen fuenf Bit der 29-bit-Kennung, `sh` die
   * unteren 24. Beim Batteriemanagement (0x17FC007B) ist cp = 17; beim
   * Klimasteuergeraet (0x00000746) ist es 00. Wer cp stehen laesst, sendet
   * an eine ganz andere Kennung und bekommt NO DATA - genau der Fehler,
   * der die Suche nach dem Ladestand aufgehalten hat.
   *
   *   cp         obere fuenf Bit der 29-Bit-Kennung (ATCP)
   *   sh         Sendeadresse (ATSH)
   *   cra        Empfangsfilter (ATCRA)
   *   fcsh       Kopf fuer die Flusskontrolle bei langen Antworten
   *   protokoll  optional: ATSP-Nummer, auf die fuer diese Abfrage
   *              umgeschaltet wird. Nur fuer 11-Bit-Steuergeraete noetig.
   */
  addresses: {
    BMS: { cp: "17", sh: "FC007B", cra: "17FE007B", fcsh: "17FC007B" },

    /* Das Klimasteuergeraet haengt an einer **11-Bit-Kennung**, nicht an
     * einer 29-Bit wie alles andere.
     *
     * 0x746 (Anfrage) und 0x7B0 (Antwort) passen in elf Bit - das ist VWs
     * klassisches Diagnoseschema. 0x17FC007B, wo Batterie und Fahrzeug
     * antworten, passt nur in 29. Der Handshake stellt `ATSP7` ein, also
     * ausschliesslich 29 Bit; eine Anfrage an 0x00000746 geht damit als
     * 29-Bit-Rahmen hinaus, und darauf hoert das Klimageraet nicht.
     *
     * Das erklaert den Befund aus der dritten Testfahrt: Aussen- und
     * Innentemperatur fehlten in **allen** 77 Runden, waehrend alles auf
     * 0x17FC.... zu 100 % ankam. Adresse und Umrechnung stimmen mit der
     * MEB-Referenz von spot2000 ueberein - es ist die Rahmenbreite.
     *
     * Ungeprueft am Fahrzeug: Es folgt aus den Adressen, nicht aus einer
     * Messung. Deshalb liegen diese Werte hinter `selten` und fallen nach
     * einem Fehlschlag fuer die Sitzung aus. */
    CLIMATE: { cp: "00", sh: "746", cra: "7B0", fcsh: "746", trace_log: "6" },

    /* Das Batteriemanagement auf der 11-Bit-Seite. Dieselbe Umschaltung
     * wie beim Klimageraet - es antwortet auf 0x77A, nicht auf
     * 0x17FE007B. */
    AKKU11: { cp: "00", sh: "710", cra: "77A", fcsh: "710", trace_log: "6" },

    // Fahrzeug-Steuergeraet: Kilometerstand und - der eigentliche Fund -
    // die Leistung der Nebenverbraucher als fertige Zahl.
    VEHICLE: { cp: "17", sh: "FC0076", cra: "17FE0076", fcsh: "17FC0076" },

    // Der DC/DC-Wandler speist das 12-V-Netz aus der Hochvoltbatterie.
    DCDC: { cp: "17", sh: "FC00B9", cra: "17FE00B9", fcsh: "17FC00B9" },
  },

  /* ===================================================================
   * Die Messwerte
   * ===================================================================
   *
   * Die Datenkennungen stammen aus der MEB-Liste von spot2000 und aus dem
   * eigenen Android-Logger; bestaetigt am Fahrzeug ist bisher nur `028C`.
   * Die uebrigen stehen ohne `pflicht` drin - schlaegt eine fehl, laeuft
   * die Aufzeichnung weiter, statt an einer Nebensache zu scheitern.
   *
   * Die Reihenfolge ist nicht beliebig: Gelesen wird von oben nach unten,
   * und ein Wechsel der Zieladresse kostet einen Umlauf ueber die serielle
   * Strecke. Werte auf derselben Adresse gehoeren deshalb beieinander, und
   * was einen Protokollwechsel braucht, steht ganz unten - geht der
   * schief, sind die Pflichtwerte dieser Runde laengst gelesen.
   */
  vals: [

    { name: "soc_raw", title: "Rohwert SoC", unit: null, put: 0,
      did: "22028C", address: "BMS", required: true,
      downhill: 0, len_total: 1 },

    { name: "voltage_v", title: "Spannung", unit: "V", put: 1,
      did: "221E3B", address: "BMS",
      downhill: 0, len_total: 2, divider: 4 },

    /* Batteriestrom.
     *
     * Ich hatte das auf die WiCAN-Formel umgestellt - fuenf Bytes ab B5 und
     * umgekehrtes Vorzeichen. Das war falsch. Zwei unabhaengige Quellen
     * nennen uebereinstimmend vier Bytes ab dem ersten Datenbyte und
     * `(Rohwert - 150000)/100`: die MEB-Liste von spot2000 und der
     * ESP32-Logger von codingABI, dessen Pufferindex nachweislich beim
     * ersten Byte nach der Quittung beginnt - also genau bei unserem b[0].
     *
     * Warum der Wert trotzdem nicht ankam, ist damit **nicht** geklaert -
     * die Formel war es jedenfalls nicht. */
    { name: "current_a", title: "Strom", unit: "A", put: 1,
      did: "221E3D", address: "BMS",
      downhill: 0, len_total: 4, pre_offset: -150000, divider: 100 },

    /* Die Energiezaehler des Fahrzeugs - der genaueste Verbrauchsmesser,
     * den es hier gibt.
     *
     * Sie zaehlen ueber die Lebensdauer, was in den Akku hinein- und was
     * herausgegangen ist. Fuer den Verbrauch zaehlt nicht ihr Stand,
     * sondern ihre **Differenz** ueber ein Stueck Fahrt - und die ist um
     * Groessenordnungen genauer als alles andere:
     *
     *     Ladestand      Schritt 0,44 pp  =  339 Wh
     *     Entladezaehler Schritt 1/8583   =  0,117 Wh
     *
     * Fast dreitausendmal feiner. Damit wird ein Balken je Minute vom
     * Rauschen zur Messung: Was eine Minute bei sechzig km/h kostet, sind
     * rund 0,25 kWh - beim Ladestand 136 % Fehler, hier 0,05 %.
     *
     * Byte-Lage und Teiler stammen aus dem ESP32-Logger von codingABI
     * (`readAndSendHVTotalChargeDischarge`); spot2000 nennt denselben
     * Teiler. Die Antwort geht ueber mehrere Rahmen - ohne Flusskontrolle
     * und Zusammensetzen kam sie ueberhaupt nicht an.
     *
     * **Vorzeichenbehaftet.** Der Entladezaehler kommt als negative Zahl -
     * am Fahrzeug gemessen 0xF7141E0D. Als vorzeichenlose 32-Bit-Zahl
     * gelesen sind das 4,15 Milliarden und damit 482 961 kWh; als
     * vorzeichenbehaftete -17 438,6, und das ist der richtige Wert.
     * Aufgefallen ist es dem Kreuzvergleich der Pruefseite: 810 kWh/100 km
     * Lebensdauerverbrauch statt der erwarteten 12 bis 40.
     *
     * `auch` holt den Ladezaehler aus denselben Bytes, statt die Abfrage
     * ein zweites Mal zu stellen - eine Mehrrahmen-Antwort kostet Zeit. */
    { name: "discharge_kwh", title: "Entladen gesamt", unit: "kWh",
      put: 2, did: "221E32", address: "BMS",
      downhill: 12, len_total: 4, sign: true, divider: 8583.07, amount: true,
      also: [
        { name: "charged_kwh", title: "Geladen gesamt", unit: "kWh",
          put: 2, downhill: 8, len_total: 4, divider: 8583.07 },
      ] },

    { name: "charge_limit_a", title: "Ladegrenze", unit: "A", put: 0,
      did: "221E1B", address: "BMS",
      downhill: 0, len_total: 2, divider: 5 },

    { name: "mode", title: "Betriebsart", unit: null, put: 0,
      did: "227448", address: "BMS",
      downhill: 0, len_total: 1 },

    /* Der Strom der PTC-Heizung. Mal Packspannung ergibt das, was die
     * Heizung allein zieht - im Winter die Frage hinter der Frage, weil sie
     * der einzige grosse Verbraucher ist, den man selbst beeinflusst. */
    { name: "ptc_current_a", title: "Heizstrom", unit: "A", put: 1,
      did: "221620", address: "BMS",
      downhill: 0, len_total: 1, divider: 4 },

    { name: "speed_kmh", title: "Tempo", unit: "km/h", put: 0,
      did: "22F40D", address: "BMS",
      downhill: 0, len_total: 1 },

    /* **Nebenverbraucher als fertige Zahl.** Alles ausser dem Antrieb -
     * Heizung, Klima, Steuergeraete, 12-V-Netz - in kW, direkt aus dem
     * Steuergeraet.
     *
     * Vorher wurde das im Stand gemessen und dazwischen fortgeschrieben:
     * Steht das Auto, ist die Packleistung die der Nebenverbraucher. Das
     * war eine brauchbare Naeherung, aber eben eine - sie galt nur so
     * lange, wie sich an der Heizung nichts aenderte, und im Fahren gar
     * nicht. Ein gemessener Wert schlaegt jede Naeherung. */
    { name: "aux_load_kw", title: "Nebenverbraucher", unit: "kW",
      put: 2, did: "220364", address: "VEHICLE",
      downhill: 0, len_total: 2, divider: 10 },

    /* Der Kilometerstand - jede Runde, und zwar direkt hinter dem
     * Nebenverbrauch.
     *
     * Er stand vorher am Ende der Liste mit `selten: 20`, wurde also bei
     * 30-Sekunden-Takt nur alle zehn Minuten gelesen. Bei den ersten
     * Testfahrten kam deshalb genau **ein** Wert an - und aus einem Wert
     * laesst sich keine Strecke bilden.
     *
     * Haeufiger zu lesen kostet hier nichts ausser der Abfrage selbst: Er
     * sitzt auf derselben Zieladresse wie der Nebenverbrauch, der ohnehin
     * jede Runde drankommt. Der Adresswechsel, wegen dessen er selten
     * gemacht wurde, faellt so gar nicht erst an.
     *
     * Aufloesung ist ein Kilometer. Fuer den Streckenanteil einer einzelnen
     * Runde ist das zu grob, fuer die Gesamtstrecke einer Fahrt genau
     * richtig - und die ist es, worauf es ankommt. */
    { name: "odometer_km", title: "Kilometerstand", unit: "km", put: 0,
      did: "22295A", address: "VEHICLE",
      downhill: 0, len_total: 3 },

    { name: "dcdc_current_a", title: "DC/DC-Strom", unit: "A", put: 1,
      did: "22465B", address: "DCDC", rarely: 10,
      downhill: 0, len_total: 2, divider: 16 },

    /* Die nutzbare Kapazitaet des Akkus, wie das Fahrzeug sie kennt.
     *
     * Interessant, weil sie mit den Jahren sinkt - und weil jeder aus dem
     * Ladestand gerechnete Verbrauch mit ihr steht und faellt. Der Wert im
     * Fahrzeugprofil ist eine Angabe aus dem Prospekt; dieser hier ist
     * gemessen.
     *
     * **Die Umrechnung ist nicht belegt.** Die MEB-Referenz fuehrt den
     * Parameter mit "equation missing"; bekannt sind nur die Einheit (Wh),
     * die Adresse und dass die Antwort vier Nutzbytes hat. Angenommen wird
     * deshalb das Naheliegende - der 32-Bit-Wert in Wattstunden. codingABI
     * rechnet `buffer2unsignedLong() / 1310.77 / 1000`, also zusammen
     * geteilt durch 1 310 770; WiCANs `[B4:B5] * 50` ist dieselbe Formel,
     * nur auf die oberen zwei Bytes verkuerzt. Vier Bytes sind feiner.
     *
     * `min`/`max` halten das Ergebnis gegen eine Plausibilitaetsgrenze:
     * Ein Autoakku hat zwischen 10 und 200 kWh. Faellt der Wert heraus,
     * stimmt die Annahme nicht, und die Zeile bleibt leer statt eine Zahl
     * zu erfinden.
     *
     * Selten gelesen, weil sie sich nicht waehrend einer Fahrt aendert. */
    { name: "battery_kwh", title: "Akkukapazität", unit: "kWh", put: 1,
      did: "222AB2", address: "AKKU11", rarely: 40,
      downhill: 0, len_total: 4, divider: 1310770, min: 10, max: 200 },

    /* Die Reichweite, die das Auto selbst ausrechnet. Interessant als
     * Gegenprobe zu jolts Prognose - dieselbe Frage, zwei Antworten.
     *
     * Die ersten beiden Datenbytes - so liest es codingABI. WiCAN nimmt
     * eines weiter; welches stimmt, sagt die erste Fahrt. Die Schranke
     * faengt den falschen Fall ab. */
    { name: "range_km", title: "Reichweite (Auto)", unit: "km",
      put: 0, did: "222AB6", address: "AKKU11", rarely: 10,
      downhill: 0, len_total: 2, min: 0, max: 999 },

    /* Die Batterietemperatur. Sie bestimmt die Ladeleistung, und bisher
     * nimmt `laden/kurven.temperatur_faktor` die **Aussen**temperatur als
     * Ersatz - der Kommentar dort sagt selbst, dass sie die Kaelte der
     * Batterie nach einer Nacht im Freien unterschaetzt. Hier ist der
     * richtige Wert. */
    { name: "batterie_c", title: "Batterietemperatur", unit: "°C",
      put: 1, did: "222A0B", address: "BMS", rarely: 10,
      downhill: 0, len_total: 1, divider: 2, offset: -40 },

    /* Die Leistung des Klimakompressors - aus zwei Messungen abgeleitet,
     * nicht aus einer Quelle abgeschrieben.
     *
     * Keine der drei Referenzen (spot2000, WiCAN, codingABI) nennt fuer
     * `220800` eine Umrechnung; spot2000 fuehrt sie als "equation missing".
     * Die Antwort traegt elf Bytes, und vier davon liegen im plausiblen
     * Wattbereich - raten waere hier besonders verlockend und besonders
     * falsch gewesen.
     *
     * Eine Differenzmessung am Fahrzeug hat es entschieden, einmal mit und
     * einmal ohne laufenden Kompressor:
     *
     *              b0    b1b2   b3b4   b5b6   b7
     *     aus    0x10       0      0      0    0
     *     an     0x51    9408   9408   2618   14
     *     (drittens, Teillast)  3648   3712   935    5
     *
     * Daraus:
     *   - `b0` Bit 0 ist an/aus.
     *   - `b1b2` und `b3b4` laufen gleich und viel hoeher - Soll- und
     *     Ist-Drehzahl. Als Watt gelesen waeren 9,4 kW fuer einen
     *     Klimakompressor zu viel.
     *   - `b5b6` ist die **Leistung in Watt**: null wenn aus, 935 bei
     *     Teillast, 2618 bei voller Kuehlung. Genau das Profil.
     *   - `b7` ist dieselbe Groesse groeber - das Verhaeltnis b5b6/b7 ist
     *     in beiden Messungen exakt 187.
     *
     * Die Drehzahl passt dazu: 3650 zu 9408 Umdrehungen ist Faktor 2,58,
     * 935 zu 2618 Watt Faktor 2,80 - naeherungsweise proportional, also
     * etwa gleiches Drehmoment. Die Schranke faengt ab, falls das an einem
     * anderen Fahrzeug doch anders liegt. */
    { name: "compressor_w", title: "Klimakompressor", unit: "W",
      put: 0, did: "220800", address: "CLIMATE", rarely: 20,
      downhill: 5, len_total: 2, min: 0, max: 8000,
      also: [
        { name: "compressor_upm", title: "Kompressor-Drehzahl",
          unit: "/min", put: 0, downhill: 3, len_total: 2 },
        // Bit 0 von b0. `maske` schneidet es heraus.
        { name: "compressor_at", title: "Kompressor an", unit: null,
          put: 0, downhill: 0, len_total: 1, mask: 1 },
      ] },

    /* Ganz zum Schluss und nur selten: Diese beiden brauchen einen
     * Protokollwechsel (siehe KLIMA). Geht der schief, sind die
     * Pflichtwerte dieser Runde laengst gelesen.
     *
     * Die Aussentemperatur ist der groesste Einzelposten der Kaelte und
     * ging bisher aus einer Vorhersage ins Verbrauchsmodell. Aus dem Auto
     * ist sie gemessen, von der Strecke, zur richtigen Zeit. */
    { name: "outside_temp_c", title: "Aussentemperatur", unit: "°C",
      put: 1, did: "222609", address: "CLIMATE", rarely: 20,
      downhill: 0, len_total: 1, divider: 2, offset: -50 },

    { name: "inside_temp_c", title: "Innentemperatur", unit: "°C",
      put: 1, did: "222613", address: "CLIMATE", rarely: 20,
      downhill: 0, len_total: 2, divider: 5, offset: -40 },
  ],
};
