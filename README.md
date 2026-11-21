# Varmemodel

Varmemodel regner din varmepumpes og dit hus' tal ud af dine egne målinger i Home Assistant. Tallene skal erstatte de faste værdier, der ellers sidder i automationer og skabeloner.

Integrationen **styrer ingenting**. Den udgiver tal som sensorer, og hvert tal viser, hvor det kommer fra: hvor mange målinger det bygger på, hvor godt det passer, hvilket udetemperatur-område det er målt i, og hvornår det sidst blev regnet ud.

## Hvad den regner ud

| Sensor | Hvad | Hvordan |
|---|---|---|
| Varmetab | W pr. grad (inde − ude), plus gratis varme i W | Varme pr. 48 timer mod temperaturforskellen. Blokke på 48 timer, fordi gulve gemmer varme fra dag til dag. Timer med varmt vand trækkes fra. |
| Effektiv COP ved 0 °C og 35 °C fremløb | COP = a + b·ude + c·fremløb | Timer hvor kompressoren har kørt hele timen uden varmt vand. **Afrimning tæller med.** Fremløbsleddet c regnes kun ud, hvis fremløbet har varieret mindst 1 grad uafhængigt af udetemperaturen. Ellers bruges en standardværdi, og det står i attributterne. |
| COP i stabil drift ved 0 °C, 35 °C fremløb og 4 kW | COP = a + b·ude + c·fremløb + d·effekt | 5-minutters portioner: tidligst 10 min efter kompressorstart (eller efter en afrimning) og indtil den afrimer, laver varmt vand eller stopper. Portionerne vægtes efter varme, og usikkerheden regnes pr. kompressorkørsel. Fremløbsleddet bruges kun, hvis det er tydeligt (mindst 2 standardfejl). Portionerne gemmes i 120 dage. |
| Tab ved opstart og afrimning | % | Målt COP over al kompressortid mod stabil COP for den samme varme. |
| COP i går / seneste 7 døgn | varme ÷ el | Det rå regnestykke: kWh varme delt med kWh el. Attributterne viser også COP uden varmt vand, hvad den stabile COP-model forventede for samme vejr og fremløb, og de seneste 14 døgn. |
| Gulvets afgivelse | kW pr. grad fremløb over rum | 25 %-fraktilen af varme ÷ (fremløb − rum) i driftstimer. |
| Maks. effekt / laveste målte effekt | kW | 90 %-fraktilen af varmen i fulde driftstimer / 10 %-fraktilen i timer, hvor kompressoren kørte hele timen. Kan kun blive lav, hvis varmepumpen faktisk har kørt lavt. |
| Varmekurve ved 0 °C | °C, plus hældning | Setpunkt minus kurve-forskydning mod udetemperatur. |
| Afrimninger pr. kompressortime | i alt og pr. udetemperatur og effekt | Talt time for time. Kun rapport. |
| Vind: ekstra varmetab pr. m/s | % | Varmetab med vind og sol: varme = H·ΔT·(1 + w·vind) − G − S·sol over 48-timers blokke. Attributterne viser solens gratis varme ved klar himmel, varmetabet i stille vejr og afkølingen i pauser pr. vindretning. Kræver vejr-entitet og ca. 30 dages opsamlede vejrdata. |
| Afkølingstid | timer | Hvor hurtigt rummene falder i pauser uden varme. |
| *Rum* afkøling (én pr. rum) | °C pr. time ved 20 graders forskel | Hvor hurtigt rummet falder, når der ikke kommer varme. Tallet falder, når rummet isoleres bedre, så det kan bruges til at se effekten af energitiltag. Lært af blokke på 6 timer: opvarmning pr. kWh nu og forsinket (gulvet), gratis varme (mennesker, madlavning) og sol. **Udluftning holdes ude**: timer efter et brat fald (≥ 0,3 °C på et kvarter) bruges ikke, og afvigere sorteres fra. Attributterne viser præcisionen over 6 timer. |
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

Opbygningen er **huset** med **varmekilder** og **rum** som underpunkter. Rummene lærer selv, hvilke varmekilder der varmer dem, og hvor meget.

**Huset**: udetemperatur (påkrævet), vejr (valgfrit, fx Met.no), antal dage i kalibreringen.

**Varmekilde** (*Tilføj varmekilde*), to typer:

| Felt | Luft-vand / vandbåren | Luft-luft |
|---|---|---|
| El (kWh, samlet) | påkrævet | påkrævet |
| Varme leveret (kWh, samlet) | anbefalet | – |
| Fremløb / retur | valgfrit | – |
| Kompressor / afrimning / varmt vand | valgfrit | kompressor / afrimning valgfrit |
| Fremløbs-setpunkt og kurve-forskydning | valgfrit (varmekurven) | – |
| Enhedens egen føler (entitet + attribut) | – | valgfrit, fx `current_temperature` |

En luft-vand-kilde får alle kalibreringstallene herunder. En luft-luft-kilde uden varmemåler får el pr. døgn, og rummene lærer, hvor mange grader de får pr. kWh el.

Sensorer med energi og temperatur skal have langtidsstatistik (`state_class`). Det tjekker opsætningen. On/off-sensorer og attributter (kompressor, afrimning, varmt vand, setpunkt, vejr) gemmer Home Assistant ikke som langtidsstatistik. Integrationen opsummerer dem derfor selv time for time og gemmer dem i 120 dage. Ved første opstart fyldes de seneste 14 dage op fra historikken.

Opgraderer du fra 0.2, flyttes opsætningen automatisk: varmepumpen bliver husets første varmekilde, og dens sensorer beholder entitets-id'er og historik.

## Rum

Tilføj hvert rum som et underpunkt (*Tilføj rum* på integrationen):

| Felt | Eksempel | Bruges til |
|---|---|---|
| Navn | Stue | enhedens navn |
| Temperaturføler | `sensor.rum_1` | rummodellen |
| Min.-temperatur (entitet eller fast) | `input_number.rum_1_min` | planlægningen (0.3) |
| Telestat / ventil | `binary_sensor.rum_1_ventil` | fordeling af husets varme på rummene |
| Kredsen er altid åben | til/fra | fx et badeværelse uden telestat |
| Relativ kredsstørrelse | 1 | 2 hvis rummet har dobbelt så mange slynger |
| Varmekilde | Luft-vand | den varmekilde, som rummets termostat eller telestat styrer (planen og fordelingen af varme bruger den; modellen lærer stadig af alle kilder) |
| Tæller med i planen | til/fra | rum der vises og læres, men ikke styres efter (fx en kælder) |
| Nabozoner | `sensor.kaelder`, `sensor.trappe` | tilstødende zoner; modellen lærer, hvor meget varme der går imellem |

Hvert rum får sin egen enhed med **Afkøling** (med hvor mange grader rummet får pr. kWh fra hver varmekilde, og hvor meget naboerne betyder) (se ovenfor) og, hvis der er telestat eller altid åben kreds, **Varme** i kWh pr. døgn (snit af 7 døgn). Varmen er husets målte varme fordelt time for time efter kredsstørrelse × minutter åben, mens kompressoren kørte. Attributterne viser andel af gulvvarmen, timer åben med varme og **brat afkøling pr. døgn** (døre, udluftning).

## Diagnostik

Diagnostikfilen indeholder kalibreringen og de brugte timer med tid som "timer før seneste time". Den indeholder ingen entitets-id'er.

## Planer

- **0.2:** Planlæggeren: de billigste timer og kurve-forskydningen for det næste døgn, ud fra tallene her.
- **0.3:** Gulvkomfort, sikkerhedsnet og en blueprint til at udføre planen.
- **0.4:** Varmekilde nummer to (luft-luft).
