# Varmemodel

Varmemodel regner din varmepumpes og dit hus' tal ud af dine egne målinger i Home Assistant. Tallene skal erstatte de faste værdier, der ellers sidder i automationer og skabeloner.

Integrationen **styrer ingenting**. Den udgiver tal som sensorer, og hvert tal viser, hvor det kommer fra: hvor mange målinger det bygger på, hvor godt det passer, hvilket udetemperatur-område det er målt i, og hvornår det sidst blev regnet ud.

## Hvad den regner ud

| Sensor | Hvad | Hvordan |
|---|---|---|
| Varmetab | W pr. grad (inde − ude), plus gratis varme i W | Varme pr. 48 timer mod temperaturforskellen. Blokke på 48 timer, fordi gulve gemmer varme fra dag til dag. Timer med varmt vand trækkes fra. |
| Effektiv COP ved 0 °C og 35 °C fremløb | COP = a + b·ude + c·fremløb | Timer hvor kompressoren har kørt hele timen uden varmt vand. **Afrimning tæller med.** Fremløbsleddet c regnes kun ud, hvis fremløbet har varieret mindst 1 grad uafhængigt af udetemperaturen. Ellers bruges en standardværdi, og det står i attributterne. |
| COP i stabil drift ved 0 °C og 35 °C fremløb | COP = a + b·ude + c·fremløb | 5-minutters portioner: tidligst 10 min efter kompressorstart (eller efter en afrimning) og indtil den afrimer, laver varmt vand eller stopper. Portionerne gemmes i 120 dage, så datagrundlaget vokser. |
| Tab ved opstart og afrimning | % | Målt COP over al kompressortid mod stabil COP for den samme varme. |
| Gulvets afgivelse | kW pr. grad fremløb over rum | 25 %-fraktilen af varme ÷ (fremløb − rum) i driftstimer. |
| Maks. effekt / laveste målte effekt | kW | 90 %-fraktilen af varmen i fulde driftstimer / 10 %-fraktilen i timer, hvor kompressoren kørte hele timen. Kan kun blive lav, hvis varmepumpen faktisk har kørt lavt. |
| Varmekurve ved 0 °C | °C, plus hældning | Setpunkt minus kurve-forskydning mod udetemperatur. |
| Afrimninger pr. kompressortime | i alt og pr. udetemperatur og effekt | Talt time for time. Kun rapport. |
| Afkølingstid | timer | Hvor hurtigt rummene falder i pauser uden varme. |
| Kalibrering | status | ok, delvis, for få data, fastfrosset eller fejl. |

Hver nat kl. 03:17 regnes alle tal ud igen af de seneste 60 dages timestatistik. Antallet af dage kan ændres. Knappen **Kalibrér nu** gør det med det samme.

### Sikkerhed mod selvforstærkning

- Et tal må højst flytte sig **±10 %** pr. kalibrering.
- Når der kommer målinger fra mindst 2 grader **koldere** vejr end før, må det flytte sig **±30 %**, så modellen lærer hurtigt, når vinteren kommer.
- Er der for få data, beholdes den sidste gode værdi.
- Tilstanden **Fastfrosset** stopper al læring, f.eks. når tallene bruges til styring.

## Installation

1. Tilføj `https://github.com/topn00b/ha-varmemodel` som brugerdefineret repository i HACS (kategori *Integration*).
2. Installér **Varmemodel** og genstart Home Assistant.
3. Gå til *Indstillinger → Enheder og tjenester → Tilføj integration → Varmemodel*.

## Opsætning

Du vælger selv dine sensorer. Kun el, udetemperatur og rumtemperaturer er påkrævet. Jo flere du vælger, jo flere tal kan regnes ud.

| Felt | Eksempel | Bruges til |
|---|---|---|
| El til varmepumpen (kWh, samlet) | `sensor.vp_el` | COP |
| Varme leveret (kWh, samlet) | `sensor.vp_varme` | varmetab, COP, afgivelse, effekt |
| Udetemperatur | `sensor.ude` | alt |
| Rumtemperaturer | `sensor.rum_1`, `sensor.rum_2` | varmetab, afgivelse, afkøling |
| Fremløb / retur | `sensor.fremlob`, `sensor.retur` | COP, afgivelse |
| Kompressor / afrimning / varmt vand | `binary_sensor.kompressor` … | fulde driftstimer, afrimninger, varmt vand |
| Fremløbs-setpunkt (entitet + evt. attribut) | `climate.vand`, attribut `temperature` | varmekurven |
| Kurve-forskydning (entitet + attribut + nulpunkt) | `climate.varmepumpe`, `temperature`, 17 | varmekurven |

Sensorer med energi og temperatur skal have langtidsstatistik (`state_class`). Det tjekker opsætningen.

On/off-sensorer (kompressor, afrimning, varmt vand) gemmer Home Assistant ikke som langtidsstatistik. Integrationen opsummerer dem derfor selv time for time og gemmer dem i 120 dage. Ved første opstart fyldes de seneste 14 dage op fra historikken.

## Diagnostik

Diagnostikfilen indeholder kalibreringen og de brugte timer med tid som "timer før seneste time". Den indeholder ingen entitets-id'er.

## Planer

- **0.2:** Planlæggeren: de billigste timer og kurve-forskydningen for det næste døgn, ud fra tallene her.
- **0.3:** Gulvkomfort, sikkerhedsnet og en blueprint til at udføre planen.
- **0.4:** Varmekilde nummer to (luft-luft).
