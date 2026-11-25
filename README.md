# Varmemodel

Varmemodel regner din varmepumpes og dit hus' tal ud af dine egne målinger i Home Assistant. Tallene skal erstatte de faste værdier, der ellers sidder i automationer og skabeloner.

Integrationen **styrer ingenting**. Den udgiver tal som sensorer, og hvert tal viser, hvor det kommer fra: hvor mange målinger det bygger på, hvor godt det passer, hvilket udetemperatur-område det er målt i, og hvornår det sidst blev regnet ud.

## Hvad den regner ud

| Sensor | Hvad | Hvordan |
|---|---|---|
| Varmetab | W pr. grad (inde − ude), plus gratis varme i W | Varme pr. 48 timer mod temperaturforskellen. Blokke på 48 timer, fordi gulve gemmer varme fra dag til dag. Timer med varmt vand trækkes fra. |
| COP-reference A0/W35 4 kW (stabil drift) | COP = a + b·ude + c·fremløb + d·effekt | Et **referencepunkt** (0 °C ude, 35 °C fremløb, 4 kW – som databladenes A0/W35), ikke det anlægget kører ved, så tallet kan sammenlignes over tid uanset vejret. Niveauet skaleres, så modellens el for portionernes varme = den målte el (`level_factor`). Bruges af planen, ganget med (1 − ekstra el ved opstart, afrimning og standby). 5-minutters portioner: tidligst 10 min efter kompressorstart (eller efter en afrimning) og indtil den afrimer, laver varmt vand eller stopper. Portionerne vægtes efter varme, og usikkerheden regnes pr. kompressorkørsel. Fremløbsleddet bruges kun, hvis det er tydeligt (mindst 2 standardfejl). Portionerne gemmes i 120 dage. |
| Ekstra el ved opstart, afrimning og standby | % | Samme regnestykke som COP i dag, over de seneste dage med 5-minutters statistik (HA gemmer den 10 dage): 1 − COP for rumvarme ÷ målt COP i stabil drift i samme periode = andelen af el ud over, hvad stabil drift ville have brugt til samme varme. Varmen fra opstartsminutterne tæller fuldt med – kun den ekstra el er tab. Modellens afvigelse fra den målte stabile drift står i attributterne. Planen ganger den stabile COP med (1 − tabet). |
| COP i dag | kWh varme ÷ kWh el | Siden midnat, regnes hvert kvarter fra 5-minutters portioner. Attributterne deler op: varmt vand (kWh varme/el og træk i %), COP uden varmt vand, stabil drift efter samme metode som ovenfor, ekstra el ved opstart, afrimning og standby i % (samme regnestykke som 14-dages-sensoren), og hvad den stabile model forventede. |
| COP i går / seneste 7 døgn | varme ÷ el | Det rå regnestykke: kWh varme delt med kWh el. Attributterne viser også COP uden varmt vand, hvad den stabile COP-model forventede for samme vejr og fremløb, og de seneste 14 døgn. |
| Gulvets afgivelse | kW pr. grad fremløb over rum | 25 %-fraktilen af varme ÷ (fremløb − rum) i driftstimer. |
| Maks. effekt / laveste målte effekt | kW | 90 %-fraktilen af varmen i fulde driftstimer / 10 %-fraktilen i timer, hvor kompressoren kørte hele timen. Kan kun blive lav, hvis varmepumpen faktisk har kørt lavt. |
| Lært fremløb ved 0 °C ude | °C, plus hældning | Målt fremløb ved ren rumvarme mod udetemperatur. |
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
| Supplerende elmåler (fx skyen) | valgfrit | valgfrit – bruges hvor hovedmåleren mangler data, skaleret til hovedmålerens niveau |
| Varme leveret (kWh, samlet) | anbefalet | – |
| Fremløb / retur | valgfrit | – |
| Kompressor / afrimning / varmt vand | valgfrit | kompressor / afrimning valgfrit |
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
| Min.-temperatur (entitet eller fast) | `input_number.rum_1_min` | planlægningen |
| Telestat / ventil | `binary_sensor.rum_1_ventil` | fordeling af husets varme på rummene |
| Kredsen er altid åben | til/fra | fx et badeværelse uden telestat |
| Relativ kredsstørrelse | 1 | kredsens andel af varmen, når den er åben. Brug helst flowet fra fordelerens flowmåler (L/min) eller slangens længde i meter – samme enhed for alle rum |
| Varmekilde | Luft-vand | den varmekilde, som rummets termostat eller telestat styrer (planen og fordelingen af varme bruger den; modellen lærer stadig af alle kilder) |
| Andre varmekilder | | kilder der også varmer rummet direkte. Rummet lærer kun af sin egen og disse kilder; varme fra andre rum læres via nabozoner (kilder der kører på samme tider kan ikke skilles ad af data alene) |
| Tæller med i planen | til/fra | rum der vises og læres, men ikke styres efter (fx en kælder) |
| Nabozoner | `sensor.kaelder`, `sensor.trappe` | tilstødende zoner; modellen lærer, hvor meget varme der går imellem |

Hvert rum får sin egen enhed med **Afkøling** (med hvor mange grader rummet får pr. kWh fra hver varmekilde, og hvor meget naboerne betyder) (se ovenfor) og, hvis der er telestat eller altid åben kreds, **Varme** i kWh pr. døgn (snit af 7 døgn). Varmen er husets målte varme fordelt time for time efter kredsstørrelse × minutter åben, mens kompressoren kørte. Attributterne viser andel af gulvvarmen, timer åben med varme og **brat afkøling pr. døgn** (døre, udluftning).

## Planlæggeren

For hver vandbåren varmekilde laves en plan for de næste op til 36 timer, hvert kvarter. Planen **styrer ingenting** – den udgives som sensorer, som en automation kan følge.

**Husets indstillinger** (*Konfigurer* på integrationen):

| Felt | Eksempel | Betydning |
|---|---|---|
| Elpris | `sensor.pris` | entitet med en liste af tid og pris pr. time eller kvarter, helst inkl. afgifter |
| Blokerede timer | `sensor.zoner` + kode `r` | timer hvor der aldrig varmes, fx dyreste pris. Format »TIMEINDEKS\|koder« eller en liste |
| »Jeg fryser« | `input_boolean.jeg_fryser` | hæver rummenes mål, så planen lægger mere varme – aldrig i blokerede timer |
| Kalender med energitiltag | `calendar.energitiltag` | før/efter-beregning af varmetabet |

**Sådan regnes planen:**

1. **Behov** pr. time = varmetab × (mål − forventet ude) − gratis varme.
2. Varmen lægges i portioner i de timer, hvor den koster mindst el (pris ÷ COP). COP falder med fremløbet, så jævn, lav drift vinder, når priserne er nogenlunde ens. Hver time med varme får mindst den laveste effekt, varmepumpen kan holde.
3. **Rummene forudsiges** time for time med rummodellerne (varme nu og forsinket, afkøling, gratis varme, nabozoner). Kommer et rum under min. + sin usikkerhed, lægges der varme i den billigste time før. Den forsinkede del af de første 6 timer kommer fra den målte varme i de seneste 6 timer, fordi gulvet afgiver den med forsinkelse.
4. For lange pauser brydes med den billigste time.
5. **Fremløbsmål** = mål + effekt ÷ gulvets typiske afgivelse (medianen), begrænset til 25–45 °C og rundet til 0,5 °C. Varmepumpen sættes i heat-tilstand og får fremløbsmålet direkte. Den anbefalede styring giver fremløbsmålet (`flow_setpoint`); det ændres højst én gang i timen og med mindst 1 °C.

**Indstillinger (tal-entiteter på huset):** *Maks. pause uden gulvvarme*, *Komfort over min.*, *»Jeg fryser« hæver målet*, *»Jeg fryser« gælder højst* (timer), *Må falde under min. i blokerede timer*, *Forsigtighed ved kulde* (%) og *(fjernet i 0.10.0)* (°C). De påvirker kun planen.

**Forsigtighed ved kulde:** er vejrudsigten mere end 1 °C koldere end det laveste, varmetabet er målt ved, regnes der med så mange % større varmetab, og rummenes margen hæves 0,2 °C. Standard er 10 %, og 0 slår det fra.

**Luft-luft i planen:** en luft-luft-varmepumpe får sin egen plan i kWh el pr. time, fordi rummene lærer grader pr. kWh el. Varmen måles ikke, så der er intet husbehov at fordele. Planen varmer kun så meget, at rummene holder gulv + komfort + usikkerhed (+ forvarmning før blokeringer), i de billigste timer før behovet. Der varmes aldrig i blokerede timer, og aldrig så meget, at et rum kommer over sit loft. Loftet sættes pr. rum som *Maks.-temperatur (loft)* i rummets opsætning; er et rum allerede over loftet, varmes der ikke, før det er faldet. Kilden får sensorerne *Plan nu*, *Plan pris* og *Anbefalet styring*. Den anbefalede styring holder mindst 30 min. tændt og 15 min. slukket og viser et setpunkt (loftet minus 0,5 °C), som enheden selv regulerer efter. Laveste og højeste el pr. time lærer planen af kildens egne driftstimer.

**Varmecentral (valgfri):** vælg rumføleren i et uopvarmet rum (kælder/teknikrum), som kun varmes af tab fra varmeinstallationen. Rummets tab til jorden, *varmetab (W/K)* × (rum − jordtemperatur), er spildvarme, som varmemåleren tæller med. Det trækkes fra, før husets eget varmetab fittes, og planen lægger det til behovet med rummets snit de seneste 7 døgn. Tabet holdes fast (sæt det ud fra U·A for vægge, gulv og dør), fordi det ikke kan skilles fra husets tab med få data. Et frit fit af begge vises som diagnose (`plant_room_free_fit`) og markeres brugbart først ved ≥ 40 blokke, ≥ 3 K spredning og |korrelation| < 0,7. Et varmere teknikrum betyder mere tab; bedre isolerede rør og beholdere sænker det.

**Lavt og jævnt (standard):** fremløbet må højst ligge på det fremløb, modellen har lært huset bruger ved timens udetemperatur. Det begrænser effekten pr. time, så varmen spredes over flere timer ved lavere effekt. Det giver lavere fremløb, højere COP, færre afrimninger og færre start/stop. Hvor meget planen må hæve over det, afgør *Prioritet* (se nedenfor) – indstillingen *Maks. fremløb over det lærte* er fjernet i 0.29.0. Det lærte fremløb er husets eget: timer, hvor Varmemodel selv satte fremløbet (*Styr fremløb* eller fuld styring), tæller ikke med – ellers lærer loftet af det fremløb, det selv har holdt nede, og falder lidt for hver nat (0.25.2). Er der for få timer uden egen styring, beholdes den seneste værdi. Efter en opdatering kalibreres der ved opstart, så en rettelse virker med det samme (0.26.0). Kan behovet ikke dækkes i de frie timer, fordi fremløbet holder effekten under varmepumpens største, står det i planens begrundelse (»ved 34 °C fremløb tager gulvet højst 3,9 kW pr. time (varmepumpen kan 5,7 kW)«).

**Kun den varme rummene behøver:** husets behov (varmetab × (mål − ude)) er kun udgangspunktet. Forudsiger rummodellerne, at rummene holder sig over kravet med mindre – fordi de er varme nu, eller gulvet har varme gemt – skæres overskuddet væk, dyreste portion først. Det giver lavere effekt og dermed lavere fremløb. Begrundelsen siger, hvor meget der er skåret. Uden rummodeller skæres intet.

**Fremløb for lavt – forudsigende:** gulvvarme er langsom (5–6 timer, før en ændring kan mærkes), så planen venter ikke på, at rummene bliver kolde. Forudsiger rummodellerne, at et rum kommer under planens grænse, fordi fremløbsgrænsen holder varmen nede (typisk sidst i en lang blokering, eller når rummene skal hentes ind bagefter), regner planen sig selv igennem med grænsen hævet 1 °C ad gangen – op til det fremløb, varmepumpens lærte største effekt giver gennem gulvet, og aldrig over det, varmepumpens fremløbsmål kan sættes til. Hvilken hævning den bruger, afgør **Prioritet** (se nedenfor). Hævningen og dens pris står i begrundelsen (»fremløbsgrænsen hævet 1 °C (balance, +5,4 kr)«) og i attributten `flow_raise`.

**Prioritet: økonomi – balance – komfort (0.26.0):** ét valg for huset (valg-entiteten *Prioritet*, på anlæg-kortet som dropdown). Der er ingen prisgrænser i koden: planen har regnet alle hævninger igennem og ved for hver af dem, hvad den koster, og hvor meget tættere rummene kommer på deres grænse. Virkningen måles pr. rum som den gennemsnitlige manko i de timer, rummet er under sin grænse – både dybde og varighed, så det tæller, at et koldt rum hentes hurtigere ind, og ikke kun hvor koldt det bliver.
- *Økonomi:* den billigste plan. Fremløbet hæves kun, når et rum derved kan holde sin grænse helt (grænsen er dit krav).
- *Balance* (standard): halvdelen af det, der kan hentes.
- *Komfort:* alt, der kan hentes – altid mindst lige så meget som balance.
- Et rum, hævningerne ikke flytter mere end følernes opløsning på 0,1 °C (fx en svag kreds), bestemmer ikke, og der vælges den laveste hævning, der når trinnets mål inden for en halv opløsning. En hævning, der gør huset koldere end uden, vælges aldrig.
- Under »søg laveste fremløb« er graderne op til loftet almindelig drift på alle trin; over loftet afgør prioriteten.
- »Plan nu« har attributten `prioritet` med de tre trins valg lige nu (`hævet`, `kr`, `kwh`, rummet der afgør det, dets laveste temperatur, tidspunkt og timer under grænsen). Anlæg-kortet viser dem som en lille tabel ved dropdownen, så man kan se, hvad et trin koster, før man vælger det. Holder rummene, er listen tom – så giver alle tre trin samme plan.

**Opvarmning efter prioritet (0.30.0):** prioriteten bestemmer også, *hvordan* planens fremløb nås – hvor hårdt varmepumpen presses, mens gulvet er koldt. Varmepumpen regulerer selv fremløbet til målet, så det, der bestemmer effekten, er **løftet**: hvor højt målet står over returen. Sammenhængen læres af husets egne 5-minutters portioner i stabil drift (median kW varme for hver hel grad løft, mindst 8 portioner pr. grad og 4 grader målt; den kræver returføler, varmemåler og et fremløbsmål, Varmemodel skriver).
- *Økonomi:* varmepumpens rolige område – løftet, der giver 15 % over den laveste effekt, den holder stabilt. Lavere taber den (og stopper/starter), højere koster mere pr. kWh varme og giver flere afrimninger. Planen regner i økonomi kun med den effekt, så den spreder varmen over flere timer og bliver senere færdig – og prioritetens tabel viser økonomi med sin egen effekt ved siden af de to andre.
- *Balance:* midt imellem den effekt og varmepumpens største.
- *Komfort:* målet direkte – varmepumpen yder alt, hvad den kan, til gulvet er med.
- Målet skrives som retur + løftet og følger returen op, mens gulvet varmes (hvert 5. minut, mens loftet holder det nede); det falder ikke, når returen dykker under en afrimning, og flyttes ikke under og lige efter varmt vand. Opstartsløft og stopværn gælder stadig, og opstartens mål tæller med, så målet ikke falder, når opstarten er slut.
- Ligger den målte effekt et kvarter under den laveste stabile (pumpen kan køre langsomt, så samme løft giver mindre), lægges 1 °C på løftet – højst 2.
- Loftet gælder også søgningens egne hævninger (rum under gulvet), så længe trinnets effekt dækker husets behov lige nu med en fjerdedel i margen. Gør den ikke det, bestemmer rummene selv – et lavt trin sulter aldrig huset. »Jeg fryser« fjerner loftet.
- Før tabellen er lært, kører alle trin som før. Anlæg-kortet viser de tre trins løft og effekt, hvad der gælder nu, og den lærte tabel (»Lært: løft → effekt«); attributten `flow_search.opvarmning` har det samme.

**Fremløb for lavt – reserve:** tager modellen fejl, så et rum er under sit gulv *og* under det, modellen ventede, mens termostaten eller telestaten har kaldt på varme uafbrudt i 6 timer, hæves fremløbsgrænsen 1 °C. Der går mindst 6 timer mellem hvert trin (højst +5 °C), og den sænkes igen 1 °C pr. 6 timer. Vises som advarsel på kortet og i attributten `flow_boost`.

**Opvarmning før blokeringer:** rummodellerne forudser selv, hvor meget hvert rum falder i en blokering, og planen lægger varme før, så rummene holder gulv + komfort + deres egen usikkerhed hele vejen igennem. Der er ingen fast forvarmning (fjernet i 0.10.0). Begrundelsen og kortet viser opvarmningen før rød.

**Søg laveste fremløb (0.16.0):** vælg *Fremløb efter start* = *søg laveste fremløb* på den vandbårne kilde. Varmepumpen starter ved retur + 3 °C (mindst 20 °C) og holder målet lavt, så længe rummene holder sig over gulvet. Målet ændres stille og roligt efter rummenes margen og målte tendens (2 timer): +1 °C pr. time, når et rum er på vej under gulvet inden for 3 timer; +2 °C pr. halve time, når et rum er under gulvet og ikke på vej op; +1 °C, når kompressoren stopper (målet er under varmepumpens laveste ydelse); −1 °C hver anden time, når alle rum er mindst 0,5 °C over gulvet og stabile – aldrig over loftet (det lærte fremløb + margen). Beder et rum om mere, mens målet står ved loftet, viser kortet »ved loftet« og siger, at loftet holder, i stedet for at love en hævning (0.25.2). **Planen regner med søgningens mål** (én sandhed): forudsigelser, »ventet«, banner og anlægsboks passer med det, der faktisk kører, og planen lægger flere timer med lavt fremløb. **Forvarmning går forud:** kan flere timer ved søgningens mål ikke holde rummene gennem en blokering, hæver planen fremløbet – op til loftet på alle trin af *Prioritet* (også lige efter en start, hvor søgningen begynder lavt – 0.24.1), og over loftet så langt, prioriteten køber (0.26.0) – vist som »forvarmer«; søgningen selv går aldrig over loftet og fortsætter bagefter. Et mål, der har holdt alle rum over gulvet i 3 timer, gemmes som *lært laveste fremløb* for udetemperaturen (5 °C-bånd). Anlæg-kortet viser status i klart sprog, mål/målt/loft, en graf 12 timer tilbage og frem (målt fremløb og retur, målet, planens timer, kompressor i drift, blokerede timer), det lærte og »Sådan virker det«; i rumlisten viser en pil rummets tendens, og ◆ markerer rummet, der bestemmer.

**Besked forud (0.19–0.20):** Varmemodel fyrer hændelsen `varmemodel_varsel` med færdige tekster – `titel`, `kort` (én linje til telefonen) og `tekst` (detaljer, markdown, til Home Assistants notifikationer) – og data (`type`, `kilde`, `problemer`, `blokeret`, ved lange røde dage også `dato` og `røde_timer`) – send den videre med en automation (se nedenfor). Tre slags (driftsbeskeder har `type: drift` – planlægningen fejler, eller varmepumpen følger ikke tænd/sluk – og kommer én gang pr. hændelse):
- **Lang rød dag** (`type: rød_dag`): røde timer pr. døgn talt pr. kvarter (blokerede timer og blokerede kvarterer, fx over et prisloft). Over 8 røde timer er der noget at være opmærksom på – **én besked pr. døgn**, så snart det er kendt (typisk når morgendagens priser kommer) og før døgnets første røde kvarter. Den nævner de røde perioder og planens laveste temperaturer for de rum, der forudsiges under grænsen, med »normalt ned til« (rummets sædvanlige laveste). Dage, der allerede er kendt, når funktionen tages i brug, springes over.
- **Problem inden for 3 timer** (`type: snart`): en nødbesked, når planen forudser et nyt, usædvanligt problem, der begynder inden for 3 timer – et rum mindst 0,2 °C under grænsen og mindst 0,3 °C koldere end dets sædvanlige dårlige dag (20 %-fraktilen af døgnenes laveste time minus gulvet, 14 døgns langtidsstatistik, regnet én gang i døgnet). Én samlet besked for huset, højst én pr. 6 timer, ingen gentagelser; rum, der allerede stod i døgnets besked, varsles ikke igen.
Plan-sensoren har attributten `problemer` (samme rum som advarslerne).

```yaml
- alias: Varmemodel – besked forud
  triggers:
    - trigger: event
      event_type: varmemodel_varsel
  actions:
    - action: persistent_notification.create
      data:
        notification_id: "varmemodel_{{ trigger.event.data.type }}_{{ trigger.event.data.dato | default('') }}"
        title: "{{ trigger.event.data.titel }}"
        message: "{{ trigger.event.data.tekst }}"
    - action: notify.notify
      data:
        title: "{{ trigger.event.data.titel }}"
        message: "{{ trigger.event.data.kort }} Detaljer i Home Assistant."
  mode: queued
```

**Varmemodel bestemmer start og stop (0.17.3–0.18.0):** start/stop slider på kompressoren, så varmepumpens egen termostat skal ikke nå at stoppe den – kun Varmemodels plan tænder og slukker. Nogle varmepumper kører lavt flow i opstarten (fx i fast flow-tilstand: ca. 9 L/min de første 7 min., fuldt flow efter ca. 17 min. – ved hver start, efter hver afrimning og efter et stop). Så skyder fremløbet 7–8 °C over returen, og kompressoren stopper ved målet + sin egen margen (ca. 4 °C) efter 4–6 min. – en kort kørsel fulgt af en pause. To værn lægges oven på søgningens mål (søgningens mål er stadig det laveste, der holder rummene, og det er det, planen regner med):
- *Opstartsløft:* når kompressoren står i varme, de første 20 min. efter en start og under og 20 min. efter en afrimning skrives mindst retur + *startløftet* (rundet op). Startløftet læres af korte kørsler (stop inden for 20 min. efter en start eller afrimning): målet, der stod i varmepumpen dengang, minus returen ved starten + 1 °C (mindst 3, højst 10 °C). En start, der kommer igennem opstarten, sænker det 0,25 °C, så det prøver lavere igen (fx efter en ændring af varmepumpens flow). Viser en lært kort kørsel sig ikke at være en (varmt vand meldte tændt lige efter stoppet – det var skiftet til varmt vand), går løftet tilbage til det, der gjaldt før; den senest lærte tjekkes også én gang efter hver opstart (0.25.1).
- *Stopværn:* mens kompressoren kører (ikke under eller lige efter afrimning eller varmt vand), holdes målet højst 2 °C under fremløbet, så fremløbet aldrig når stopgrænsen – heller ikke på laveste effekt, hvor huset ikke kan tage hele ydelsen. Målet følger kun fremløbet op, aldrig over loftet.
- Målet skrives straks, når kompressoren stopper eller starter, en afrimning begynder eller slutter, eller fremløbet nærmer sig stopgrænsen – ikke først ved næste kvarters planlægning (varmepumpen genstarter typisk 3 min. efter et stop). Et Modbus-udfald (»utilgængelig«) er intet skift.
- Et stop efter længere drift (ikke opstart) hæver stadig søgningens mål 1 °C efter en halv time.
Attributten `flow_search` viser `værn` (opstart/stopværn og målet), `startløft`, `korte_kørsler`, `sidste_korte`, `mål_vp` (målet, der står i varmepumpen nu) og `mål_kl` (hvornår et nyt mål når frem – planens fremløb ændres højst hvert kvarter, ved næste planlægning); kortet viser »Mål i varmepumpen« som det, der faktisk står i varmepumpen, med fx »→ 35 °C kl. 13:47 · forvarmer« som undertekst (0.18.1). Rettet i 0.17.3: tællingen af kompressorstop læste historikkens første række (tilstanden ved forespørgslens start) som »afrimningen sluttede lige« og overså stop i vinduets første 10 min.

**Lær stigen fra historikken (0.17.0):** luft-luft-stigens virkning pr. trin (°C pr. kWh el) læres også fra de seneste 14 døgns historik – med samme beregning som den løbende læring og stramme filtre: kun kvarterer, hvor trinnet har kørt mindst 45 min. og varmepumpen varmer; ingen afrimning, ingen udluftning (et rum falder > 0,3 °C på et kvarter), ingen skift i ventilationen; enkeltmålinger, der stikker voldsomt ud for samme rum og trin, kasseres (median ± 3 × MAD). Kun kvarterer, hvor stigen selv styrede (*Varmemodel styrer* til) og blæseren stod på et af stigens trin (ikke »Auto«), bruges – data fra en anden styring er ikke stigens trin. Perioder med ordet *ignorer* i tiltags-kalenderen (fx en dørtest) springes over – og tælles ikke som energitiltag. Køres automatisk første gang; knappen *Lær stigen fra historikken* kører den igen.

**Prisloft i fanen (0.30.0):** regnes de blokerede kvarterer efter et prisloft i din egen tal-hjælper, kan den vælges i opsætningen (*Prisloft (tal-hjælper)*). Så viser anlæg-kortet loftet som en dropdown (0,25 kr-trin), der skriver til hjælperen, og hvor mange kvarterer der ligger over det – så tallet ikke er gemt.

**Varmemodel mod jævn drift (0.30.0):** hvad havde den samme varme kostet, hvis varmepumpen bare havde kørt jævnt? For hvert afsluttet døgn med priser i planloggen gemmes én række: Varmemodels faktiske drift (målt el × timens pris), varmen lagt jævnt over døgnet, og jævnt med varmepumpen slukket kl. 17–21. De to jævne regnes med husets egen COP-model ved timens udetemperatur og det fremløb, effekten kræver – dog mindst varmepumpens laveste effekt (under den kører den i ryk) – og med rimens faktor. Kun rumvarme i timer uden varmt vand, ens for alle tre, og kun døgn med mindst 18 sådanne timer. Som kontrol står også, hvad samme model giver for Varmemodels egne timer. Attributten `sammenligning` på *Planens pris* har i går, de seneste 7 og 30 døgn og døgnene enkeltvis (62 døgn gemmes); anlæg-kortet viser det i boksen *Varmemodel mod jævn drift*. Jævn drift holder rummene varmere om dagen – oversigten sammenligner kun prisen for den samme varme.

**Blokerede kvarterer (0.16.5):** vælg en sensor med attributten `kvarterer` (starttider for blokerede kvarterer, fx over et prisloft). En time med mindst 2 af dem planlægges som blokeret, så varmen lægges i timerne før – i stedet for en kørsel, der klippes over midt i timen. *Blokering lige nu* stopper stadig varmen præcist i de enkelte kvarterer. **Delvist frie timer (0.25.0):** ligger de blokerede kvarterer samlet i den ene ende af timen, og er mindst to kvarterer frie (rødt fra 06:30 eller rødt til 21:30), bruger en vandbåren kilde den frie del – men kun som fortsættelse af nabotimen på den frie side, så varmepumpen ikke stopper en halv time før blokeringen efter en hel nats forvarmning og aldrig starter for en halv times skyld. Timen har `free` (fx 0,5) i planen, varmen i den er timens kWh ved samme effekt og fremløb som nabotimen, prisen er de frie kvarterers (når prislisten har kvarterspriser), *næste stop* og opvarmningsvinduet viser det røde kvarter (06:30), og styringen er blokeret i de røde kvarterer – også uden en *Blokering lige nu*-entitet. Luft-luft-kilder regner stadig hele timen som blokeret. Rødt vises med kvarterer (0.17.2): *Plan nu* har attributten `røde_perioder` (timeplanens blokerede timer + de blokerede kvarterer, fx 06:30–10:15), kortet viser rød start og slut med minutter og tegner rødt pr. kvarter i fremløbsgrafen, og *næste start* flyttes forbi et rødt kvarter (planens time kl. 10, men 10:00–10:15 rødt → start 10:15).

**Blokering lige nu (0.15.0):** ud over timeplanen (*Blokeringsentitet*) kan en entitet blokere lige nu – fx en prisstatus, der er rød, når kvarterets pris er over et loft. Varmepumperne slukkes/startes ikke, mens den står i en af *Blokerende tilstande*, og planen regnes straks igen, når den skifter.

**Prioritet, luft-luft (0.29.0; før »Opstart« rolig/hurtig, 0.16.0):** hver luft-luft-kilde har samme valg som huset og varmt vand – *økonomi*, *balance* eller *komfort* – så kompromiset vælges ens alle tre steder. Det afgør, hvor hårdt stigen går til, når et rum er under sit gulv:
- **Komfort** (før *hurtig*): et trin op hvert kvarter, til rummet er over gulvet.
- **Balance** (før *rolig*, standard): stigen starter ved planens trin (det trin, hvis lærte forbrug dækker planens varme) – højst midt på stigen, til virkningen pr. trin er lært – og kører aldrig under planens trin, mens et rum er under gulvet. Hvert trin holdes mindst en halv time, og der går kun et trin op, når det koldeste rum under gulvet ikke stiger mindst 0,2 °C/t (målt de seneste 30 min.). Et højere trin, der ikke er afprøvet, sættes ned og prøves først.
- **Økonomi:** som balance, men trinnet holdes, så længe det koldeste rum stiger mindst så hurtigt, som planen selv regner med i den kommende time (planen har valgt timer og mængde efter pris) – aldrig strengere end balance. Rummet kommer senere op, for mindre el.

Planen bruger stigens lærte virkning pr. trin (°C pr. kWh el): giver de høje trin mindre varme pr. kWh el, spreder den varmen over flere timer på lavere trin (mindre el); indtil mindst tre trin er lært (med egne eller delte tal – ikke skøn), regner den for økonomi og balance med højst midterste trin. *Jeg fryser* bruger altid øverste trin. Et gammelt valg oversættes selv (rolig = balance, hurtig = komfort), og den gamle entitet fjernes.

**Blød start (0.14.0):** vælg *Blød start* fra / til / test på den vandbårne kilde (kræver *Varmemodel styrer varmepumpen*). Ved hver tænding skrives fremløbsmålet først til retur + 3 °C (mindst 20 °C) – før varmepumpen tændes – og hæves derefter 2 °C pr. kvarter til planens fremløb. Kortet viser målet, der faktisk står i varmepumpen (»mål 26 → 35« under blød start) – så kompressoren ikke går til fuld effekt mod et koldt gulv (højere COP og færre afrimninger i kulde). *test* gør hver anden start blød og måler COP og afrimninger de første 90 min.; resultatet for ude under 6 °C står i entitetens attributter.

**Varmemodel styrer varmepumpen (vandbåren, 0.13.0):** vælg også *Tænd/sluk* (varmepumpens climate-entitet) i opsætningen. Så får kilden kontakten *Varmemodel styrer varmepumpen*: Varmemodel tænder (varme) og slukker efter den anbefalede styring (mindst 60 min. tændt, 30 min. slukket, højst én ændring pr. 15 min., slukker straks i blokerede timer, højst 10 skift pr. døgn) og skriver fremløbet. Termostater, varmt vand og cirkulation styres fortsat af egne automationer; slå din egen tænd/sluk-automation fra, mens kontakten er til, og lad en vagt slå kontakten fra og din automation til ved fejl.

**Isvagt (0.27.0):** en luft-vand-varmepumpe kan ise til, når afrimningerne ikke får lamelfladen ren – så vokser isen fra gang til gang, og til sidst sidder den i en isblok. Føleren *Isvagt* (på en vandbåren kilde med kompressor- og afrimningsføler) holder øje med det ud fra de samme 5-minutters portioner som COP i stabil drift:
- En **driftscyklus** går fra en afrimning eller start til den næste afrimning, varmt vand eller stop. For hver cyklus måles ydelsen i procent af husets egen COP-model (samme udetemperatur, fremløb og effekt): *ren flade* de første 15 minutter, *sidst* de sidste 10 minutter, når rimen har sat sig, og *snit* over hele cyklussen.
- Der er ingen fast grænse for ydelsen: en cyklus er **lav**, når den rene flade eller snittet er ringere end ni ud af ti af husets egne cyklusser de seneste 30 døgn (uden de seneste 12 timer). Enkelte lave cyklusser er almindelige – tung rim en fugtig aften klarer afrimningen selv.
- Det usædvanlige er, at det bliver ved: tre lave i træk vises som `watch` (»følg«), og lave cyklusser i træk i seks driftstimer er `ice` (»is?«). Pauser tæller ikke med – er varmepumpen iset til, er den det også efter en rød periode. Mens et løb af lave cyklusser varer, holdes grænserne fast, så en lang tilisning ikke selv flytter grænsen for, hvad der er lavt.
- Ved `ice` sendes én driftsbesked (`varmemodel_varsel`, `type: drift`) med tallene; en ny sendes først, når ydelsen har været normal igen.
- Attributterne har de seneste cyklusser (`start`, `min`, `ude`, `kw`, `ren_pct`, `slut_pct`, `snit_pct`, `lav`), husets normale og lave niveau, antal lave i træk og døgnets afrimninger (antal, typisk afstand og varighed). Anlæg-kortet viser det i boksen *Isvagt*.
- **Rimens pris (0.29.0):** under en afrimning vender varmepumpen kredsen og tager varmen til at smelte rimen fra gulvvandet – fremløbet er koldere end returen. Varmemåleren tæller ikke baglæns, så den varme ses ikke i nogen COP. Isvagten måler den for hver afrimning uden flowmåler: vandets flow skønnes af varmeeffekten ÷ (frem − retur) lige før afrimningen, og (frem − retur) lægges sammen, til fremløbet igen er varmest (kræver *Returtemperatur*). Medianen over de seneste 30 døgn (`afrimning_tager_kwh_varme`) og døgnets sum (`rim_24t_kwh_varme`) står på isvagten. Kalibreringen lærer, hvor mange afrimninger der er pr. kWh varme i hvert udebånd (under 0, 0–3, 3–6, over 6 °C; bånd med under 20 kWh varme tæller ikke). Sammen giver de planens faktor (`rim_faktor`): hvor meget af den målte varme huset reelt får ved udetemperaturen i forhold til gennemsnittet – husets og rummenes modeller er lært af den målte varme og har gennemsnittets rim med i sig. I rimvejr rummer en time tilsvarende mindre og koster mere pr. kWh, huset får (planens timer har `frost`, og `cop` er for den varme, huset får); i mildt vejr omvendt. Der er ingen faste tal i det.
- **Faldet frem mod afrimningen (0.30.0):** det dyre er ikke kun selve afrimningen, men at ydelsen falder, mens rimen sætter sig. Isvagten viser den rå COP de første 15 minutter af en cyklus mod de sidste 10, delt i husets rolige og hårde cyklusser (under/over median-effekten, kun rimvejr og cyklusser på mindst 30 min., mindst 6 i hver gruppe) – attributten `ydelse_mod_afrimning` og en linje i boksen *Isvagt*.
- **Isvagten holder igen (0.29.0):** står isvagten på `watch` eller `ice`, hæver planen ikke fremløbet over det almindelige loft, uanset *Prioritet* – en flade, der ikke bliver ren, skal ikke presses hårdere. Det står i planens begrundelse og på isvagten (`fremløb_holdes`), og det slipper af sig selv, når ydelsen er normal igen.
- Regnes hvert kvarter. I et hus, hvor varmepumpen frøs til i februar 2026, faldt ydelsen jævnt i ca. 30 timer før optøningen – så længe i forvejen kan det ses.
- **Luft-luft uden afrimningsføler (0.27.1):** afrimningerne læses af el-effekten – et kort dyk midt i driften, hvor effekten falder til under halvdelen i 1–12 minutter, og varmepumpen kører videre bagefter. En kort slukning (helt ned på den effekt, varmepumpen bruger slukket) og et stop, fordi rummet er varmt nok, tæller ikke. Trin-føleren har `afrimninger_24t`, `seneste_afrimning` og `afrimninger` (start, minutter, laveste W), og luft-luft-kortet viser antallet. Kræver en effektføler (W) på varmekilden.

**Varmt vand (0.28.0; styring 0.29.0):** vælg *Varmtvandstankens temperatur* på den vandbårne kilde, så lærer Varmemodel tanken og lægger en plan for varmt vand. Føleren *Varmt vand* viser planen (`off`, `normal`, `high`) ved siden af det, varmepumpen faktisk står på. Planen er en anbefaling, indtil kontakten *Varmemodel styrer varmt vand* slås til.
- **Tanken læres af målingerne** (de seneste ti døgn, en gang i døgnet): tabet i ro (som andel af tankens overtemperatur – lært af sammenhængende blokke på fire rolige timer, og ikke af timen lige efter en opvarmning, hvor føleren halter), forbruget time for time på døgnet, hvor hurtigt varmepumpen varmer tanken (°C pr. minut), hvad en grad koster i el, hvor langt under målet tanken skal være, før varmepumpen går i gang (startgrænsen), og hvor længe kompressoren står, når en opvarmning slutter (pausen). Til der er data nok, regnes der med standardtal – attributten `tank_model` viser, hvad der er lært og af hvor meget.
- **Brugstiden (0.29.0):** grænsen »mindst« gælder kun, når vandet bruges. Brugstiden læres af tankens fald (timer på døgnet med målbart forbrug) – eller sættes med *Varmt vand bruges fra* og *til* (står de ens, bruges den lærte). Uden for brugstiden må tanken gerne være kold: der varmes ikke om aftenen til en nat, hvor ingen bruger vand, men tæt på morgenen – og tanken er klar, *før* brugstiden begynder, ikke først efter en opvarmning i selve timen.
- **Planen** vælger for hver time fra, »normalt« eller »højt«: først så tanken er mindst *Varmt vand: mindst*, når en time i brugstiden begynder (et bad må gerne tage den under – så varmes den i næste frie time), dernæst billigst – aldrig i blokerede timer. Det giver af sig selv en opvarmning tæt på en lang blokering, så tanken går fuld ind i den. Det, der står i tanken ved periodens slutning, regnes ikke som spildt – og det, der mangler, regnes som en regning, der kommer bagefter.
- **De eneste faste tal er dine egne tre temperaturer** (tal-entiteter, standard 40 / 45 / 50 °C): *mindst*, *normalt* og *højt*. **Varmt vand har sin egen prioritet** (*Varmt vand prioritet*, 0.29.0): økonomi varmer kun til »normalt«; balance må også bruge »højt« (fx lige før en blokering, tanken ellers ikke kan holde igennem, eller når det samlet er billigere at fylde mere på i en billig time); komfort holder desuden tanken tæt på »normalt« i brugstiden og aldrig under »mindst« – heller ikke om natten.
- **»Bad nu«** (valgfri entitet): mens den er tændt, er varmt vand »højt« med det samme – også i en blokeret time.
- **Styringen (0.29.0):** med *Varmemodel styrer varmt vand* slået til skrives planen til varmepumpen – målet først, så til; kun ved en forskel. Det kræver *Varmt vand til/fra på varmepumpen* og en *måltemperatur*, der kan sættes (climate, water heater eller tal – en føler viser kun målet). Målet holdes inden for det, entiteten tager imod. I en blokering (også et dyrt kvarter lige nu) er varmt vand fra – undtagen »bad nu«. En opvarmning, der er i gang, får lov at køre færdig, når planen skifter til fra uden for en blokering; planen regner den færdig i stedet for at melde »fra« midt i den. Tre minutter efter en skrivning tjekkes det, at varmepumpen står som ønsket; gør den ikke det – eller svarer den første entitet ikke – skrives der til *reserven* (fx producentens sky), hvis den heller ikke viser det ønskede. Højst 48 skrivninger i døgnet. »Bad nu« og en opvarmning, der begynder eller slutter, giver en ny plan med det samme.
- **Varmeplanen regner med varmt vand (0.29.0):** når Varmemodel styrer varmt vand, lægges planen for varmt vand først, og de timer, hvor varmepumpen laver varmt vand, rummer tilsvarende mindre rumvarme (opvarmningens minutter plus pausen bagefter) – planens timer har så `dhw` (andelen af timen).
- Attributterne har planen time for time (`mål`, `varmer`, minutter, kWh el, kr, tanken ved timens start og slut, `brugstid`), næste opvarmning, tankens laveste punkt i brugstiden, timer under »mindst«, `brugstid` (timerne) og `brugstid_sat`, `styrer`, `seneste_skrivning`, `reserve_brugt` og den lærte model. Anlæg-kortet viser det i boksen *Varmt vand*, hvor prioriteten, styringen, de tre temperaturer og brugstiden også kan rettes.

**Dagens driftstal (0.24.0):** *COP i dag* har attributten `drift`: `starter` (kompressorstarter siden midnat – genstarten inden for 10 min. efter et ventilskift, en afrimning eller varmt vand er samme kørsel og tælles ikke, 0.25.0), `stop` og `korte_kørsler` (stop inden for 20 min. efter en start eller afrimning – uden ventilskift, afrimning og varmt vand; skiftet til varmt vand, hvor kompressoren står 3–4 min., og varmt vand melder tændt op til 2 min. efter stoppet, er heller intet stop, 0.25.1), `afrimninger`, `varmt_vand`, `drift_min` (kompressorens driftstid) samt Varmemodels egne skrivninger (`tænd_sluk_skrevet`, `fremløb_skrevet`) og `plan_frisk_pct`. Anlæg-kortet viser dem under varmepumpen: »i dag: 3 starter · 0 korte kørsler · 5 afrimninger · drift 9 t 20 min« – korte kørsler fremhæves, fordi start/stop slider på kompressoren.

**Robust styring (0.22.0):**
- **To veje til tænd/sluk:** vælg også *Tænd/sluk – reserve* (en anden climate-entitet for samme varmepumpe, fx lokal Modbus som primær og producentens sky som reserve). Varmemodel skriver til den første, der svarer. Tre minutter efter en tænd/sluk tjekkes det, at varmepumpen står som ønsket; viser en af entiteterne det modsatte, får den kommandoen direkte. Ved en blokering slukkes der ad begge veje med det samme (0.25.1) – en sky viser først skiftet efter flere minutter, og et sikkerhedsnet udefra må ikke nå at se varmepumpen som tændt. Efter tre forgæves tjek kommer der én besked. Svarer ingen af dem under en blokering, prøves der igen hvert minut.
- **Tilbage til varme-tilstand:** står varmepumpen tændt i en anden tilstand (fx »auto« efter en reserve-styring), sættes den tilbage til varme – fremløbsmålet gælder kun dér.
- **Efter en genstart** venter planlægningen op til 2 minutter på pris- og blokeringsentiteterne. En plan uden dem (nødplan, intet blokeret) styrer, logges og varsler ikke i den tid.
- **Fejler planlægningen,** styres der videre efter den seneste gode plan (dens timer rækker et døgn eller mere), og en blokering slukker stadig. `updated` bevares, så en vagt kan se, hvor gammel planen er. Har det varet et kvarter, kommer der én besked (`type: drift`).

**Varmemodel styrer fremløbet (vandbåren, 0.12.0):** vælg *Fremløbsmål* (fx varmepumpens climate-entitet) i varmekildens opsætning. Så får kilden kontakten *Varmemodel styrer fremløb*. Når den er til, skriver Varmemodel planens fremløbsmål – kun når varmepumpen står i varme-tilstand, aldrig i blokerede timer, kun ved en ændring på mindst 1 °C og højst hvert kvarter (loft 96 pr. døgn som sikring). Tænd/sluk og termostater styres stadig af din egen automation (trin P1 i en gradvis overtagelse). Uden planlagt varme lige nu bruges planens næste fremløb, så en varmepumpe, der startes af anden styring, kører lavt.

**Energitiltag pr. etage (0.11.1):** skriv varmekildens navn, et rums navn eller »1. sal« i kalender-begivenhedens titel eller beskrivelse, så måles tiltaget på den etage. Vandbåren kilde: etagens varmetab (W/K) fra den målte varme. Luft-luft: etagens el pr. grad (W el/K) – varmen måles ikke, men ændringen i % viser, om tiltaget virker. Uden etage måles på den vandbårne kilde.

**Økonomi-knappen (0.13.1):** *Økonomi: må gå under gulvet med* (0–3 °C) accepterer, at rummene altid må ligge op til så meget under deres gulv. Planen køber så mindre varme, ved lavere effekt og dermed lavere fremløb – modellen finder selv det laveste fremløb, der holder rummene inden for det accepterede. Begrundelsen viser valget. Forbrug og virkning pr. luft-luft-trin læres nu også pr. 5 °C udetemperatur (samlede tal bruges, til et bånd har data).

**Økonomi-valg (0.10.2):** *Må gå under gulvet i røde timer med* gælder nu både vandbårne og luft-luft-kilder. Sæt den fx til 0,5 °C, så accepterer du, at rummene bliver op til så meget koldere end gulvet i blokerede timer – til gengæld købes der mindre (dyr) varme før, og luft-luft-stigen forvarmer tilsvarende mindre.

**Data som de er (0.10.0):** usikkerheden i et rum er rummodellens afprøvede præcision over 6 timer (mindst 0,1 °C) – ingen faste tillæg ved dårlig model eller kulde. Komfort er et bånd (0.11.0): *Rum må svinge over gulvet med* er, hvor meget rummene må ligge over gulvet – ikke et mål, planen køber varme for at nå (det gjorde planen 50 % dyrere uden mere komfort). Kravet er gulv + rummodellens usikkerhed, der vokser fra 0 i timen nu til den fulde 6-timers præcision. Rummodellerne regner ændringen over de samme 6 timer som tilførslen (før blev alle rum forudsagt ca. 17 % for langsomt), og vagten holder en rummodel samlet (værdi og koefficienter). En rummodel regnet med en ældre metode erstattes uden ±10 %-grænsen.

**»Jeg fryser«** gælder fra den slås til, i højst det valgte antal timer og kun til næste blokerede periode begynder. Der varmes aldrig i blokerede timer.

**Log og kontrol:**
- Ved hver times start gemmes, hvad planen sagde for timen.
- Næste nat sammenlignes gårsdagen: planlagt mod faktisk varme, el, kroner og varme i blokerede timer (attributten *i_går* på *Plan pris*).
- Hvert rums forudsigelse fra 6 timer før sammenlignes med den målte temperatur. Afviger den mere end 2 × rummets præcision (mindst 0,3 °C), markeres rummet (attributten *rooms_check* på *Plan nu*). Koldere end ventet peger på en åben dør eller et vindue; varmere end ventet på ekstra varme fra sol, mennesker eller en varmekilde, der kører uden om planen.

**Anbefalet styring (sensor pr. vandbåren kilde):** viser, hvad en automation bør sende nu, efter regler der skåner varmepumpen. Den styrer ikke selv noget.

Reglerne:
- aldrig varme i blokerede timer; slukker straks
- mindst 60 min. tændt og 30 min. slukket
- højst én ændring pr. 15 min.
- fremløbsmålet holdes mindst 1 time og ændres med mindst 1 °C

Attributterne tæller pr. døgn skrivninger, tænd/sluk-skift og varme i blokerede timer. *within_limits* er sand, når der er højst 8 tænd/sluk-skift og ingen varme i blokerede timer (justering af fremløbet skåner ikke noget).

**Nødplan:** mangler elprisen helt, planlægges med flad pris, så rummene stadig holdes over komfortgulvet. Det står i begrundelsen, og *inputs_ok* er falsk.

**Tjek af forudsigelsen:** hver time tjekkes rummene mod det, rummodellen forudsagde for 6 timer siden.
- Det *ventede* regnes fra rummets temperatur dengang med den varme, der faktisk kom (*grundlag: målt varme*). For rum på en luft-luft-kilde uden varmemåler bruges den målte el (*målt el*). Så tjekkes modellen også, når planen ikke blev fulgt, fx mens en automation styrer efter noget andet.
- Planens egen forudsigelse står som *ventet_med_plan*.
- Status er *ramt*, *over* eller *under*. Afviger et rum mere end grænsen, vises det som afvigelse.
- *Ramt* er inden for rummets margen. *Grænsen* er 2 × rummets ærlige præcision, dvs. den største af fittets og den afprøvede. Margenens loft på 0,6 °C gælder kun planen, så et svagt rum (fx ±1,2 °C) ikke giver falske afvigelser.

**Følere der falder ud:** svarer en rum- eller udeføler ikke, bruges den sidst kendte værdi i op til 30 min. Korte udfald, fx når en skytjeneste er nede et øjeblik, giver ingen alarm. Rummet står under *rooms_held* på *Plan nu*.

**Over 30 min. uden måling tager forudsigelsen over:**
- Rummets temperatur regnes frem fra sidste måling i 5-minutters trin med den samme rummodel som planen. Den bygger på den varme eller el, kilden faktisk gav (5-minutters statistik), udetemperaturen og naboerne.
- Planen og *Anbefalet styring* kører videre på forudsigelsen. Rummet står under *rooms_estimated* med forudsagt temperatur, sidste måling, minutter og usikkerhed.
- Usikkerheden vokser, jo længere rummet er uden måling. For hver 6 timer ud over de første 6 lægges rummets præcision til, dog højst til 1 °C. Planen varmer derfor hellere lidt for meget end for lidt.
- Udeføleren erstattes af vejrudsigten for den aktuelle time.
- Imens logges og tjekkes rummets forudsigelse ikke, fordi startpunktet ikke er målt.

**Mangler data** (problem-sensor på huset) slår til, så det kan udbedres hurtigst muligt:
- Den er tændt, så længe et datapunkt har manglet i over 30 min. De første 10 min. efter en genstart slår den ikke til, fordi andre integrationer måske ikke er klar endnu. Attributterne viser hvert rum og udeføleren med føler, sidste måling, minutter og det, planen bruger i stedet.
- Hvert manglende datapunkt får også en reparation under *Indstillinger → System → Reparationer*. Den forsvinder af sig selv, når føleren måler igen.
- Brug sensoren i en automation, hvis du vil have besked på telefonen.
- Kortene viser rummet med »≈« og en linje under afvigelser.
- Et rum, der aldrig har målt eller ikke har en rummodel endnu, kan ikke forudsiges. Det udelades og står under *rooms_skipped*.

**Huller og sommertid:**
- Planen regner i UTC-timer.
- Mangler prisen for én enkelt time, bruges snittet af nabotimerne. Det sker fx, når uret stilles tilbage, og to lokale timer hedder det samme.
- Nye priser, typisk kl. 13, kommer med ved næste planlægning, senest efter 15 min.

**Frisk plan:** *fresh_pct_today* på *Plan nu* er den andel af dagens kvarterer, der havde en frisk plan med alle input.

**Usikkerhed:** *rooms_band* på *Plan nu* er ± usikkerheden for hvert rums forudsigelse.

**Energitiltag:** Vælg en kalender under *Konfigurer*. Hver begivenhed er et tiltag. Ved kalibreringen regnes varmetabet for sig før og efter datoen (dagen selv udelades). Det kræver ca. 16 døgn på hver side. Resultatet ses på sensoren *Energitiltag*.

**Sensorer pr. varmekilde:** *Plan nu* (varme / slukket / blokeret, med effekt, fremløb, pris, grund, næste start/stop, hele planen og rummenes forudsigelse i attributterne), *Plan fremløb* og *Plan pris* (forventet elpris for perioden).

Til planlægning bruges COP-formlen fra *COP i stabil drift*, men højere effekt eller fremløb må aldrig gøre COP bedre: et positivt effektled holdes fast ved referenceeffekten (det skyldes typisk koldt gulv efter pauser, ikke effekten selv).

## Kort til dashboardet

**Opdatering af kortene (0.22.2):** efter en opdatering kører en åben browser eller app stadig de gamle kort, til siden genindlæses. Kortene tjekker selv integrationens version og viser så en linje øverst: »Kortene er opdateret til … – tryk her for at genindlæse«. Kortene tegnes kun om, når indholdet har ændret sig, og brede tabeller beholder deres rulleposition.

Integrationen leverer seks kort, som selv finder Varmemodels entiteter (Hus → varmekilde → rum). Tilføj dem med *Tilføj kort* eller i YAML:

| Kort | Viser |
|---|---|
| `custom:varmemodel-anlaeg-card` | anlægget nu: varmekilde, kredsløb, rum, planen på en tidslinje og afvigelser |
| `custom:varmemodel-rum-card` | rummene: målt, forudsagt for 6 timer siden (ramt, over eller under), de næste timer og rummodellens tal |
| `custom:varmemodel-kilde-card` | varmekildernes tal: effektivitet, COP for 14 døgn, bygning og anlæg |
| `custom:varmemodel-tiltag-card` | energitiltag: hvilke rum taber varmen hurtigst, vaner, vind og før/efter |
| `custom:varmemodel-hus-card` | status, planen og planens indstillinger |
| `custom:varmemodel-plan-card` | rummene på din plantegning farvet efter temperatur, nu og de næste timer |

Kortene tilføjes automatisk som dashboard-ressource, så de også indlæses i mobilappen. Har du dashboards i YAML-tilstand, skal du selv tilføje `/varmemodel/varmemodel-cards.js` som ressource af typen *module*.

**Pålidelighed:** sikre tal står som de er, foreløbige har »≈« foran, usikre er nedtonede, manglende er »–«. Tryk på et tal for detaljer. Følere, der falder kortvarigt ud, vises med seneste kendte værdi i op til 30 min.

**Anlægskortet** tegnes efter dit anlæg. Alt er valgfrit:

```yaml
type: custom:varmemodel-anlaeg-card
buffer: true          # buffertank i returen
bypass: true          # kortslutning (T-stykke) mellem fremløb og retur
dry_run: true         # planen styrer ikke endnu
zone: sensor.price_level        # prisniveau (rød/gul/grøn)
price: sensor.electricity_price # elpris nu
extra:                # op til to ekstra temperaturer i ude-boksen
  - name: Kælder
    entity: sensor.basement_temperature
notes:                # tekst fra en entitet, fx et udluftningsråd
  - entity: sensor.airing_advice
    attribute: text
flags:                # vises i prisfeltet, når entiteten har tilstanden
  - entity: input_boolean.circulation_stopped
    state: "on"
    text: Cirkulation stoppet
alerts:               # egne afvigelser
  - entity: automation.heating
    state: "off"
    text: Varmeautomatikken er slukket
  - entity: input_text.heating_watchdog   # `not`: alt andet end de nævnte tilstande; {state} sætter tilstanden ind
    not: normal
    text: "Vagten: {state}"
actions:              # knapper (slår til/fra)
  - entity: input_boolean.im_cold
    name: Jeg fryser
sources:
  Heat pump:          # varmekildens navn i Varmemodel
    climate: climate.heat_pump
    heat_power: sensor.heat_power       # varmeeffekt nu (kW eller W)
    el_power: sensor.heat_pump_power    # eleffekt nu (kW eller W)
    flow_rate: sensor.flow_rate         # L/min
    tank: sensor.hot_water_temperature
    pump: input_select.pump_speed       # cirkulationspumpe efter kortslutningen
    pump_running: binary_sensor.pump_running
    a: sensor.pipe_heat_pump_out        # følere: A varmepumpe ud, B manifold ind,
    b: sensor.pipe_manifold_in          #         C manifold retur, D varmepumpe ind
    c: sensor.pipe_manifold_return
    d: sensor.pipe_heat_pump_in
  Air to air:
    climate: climate.air_to_air
    el_power: sensor.air_to_air_power
```

Med `source: <varmekildens navn>` viser kortet kun den varmekilde: dens rum, plan, tidslinje og afvigelser. Så kan anlægget deles op i én visning pr. varmepumpe eller etage. En luft-luft-kilde tegnes med varmepumpen til venstre og rummene ved siden af.

**Dashboard på ét klik:** opret et nyt dashboard i HA, vælg »Rediger → Rå konfiguration« og skriv `strategy: {type: custom:varmemodel}`. Varmemodel bygger dashboardet ud fra det, den kender: én fane pr. varmekilde (anlæg, plan, afvigelser og indstillinger), rum, varmekilder, energitiltag og hus. Kort-indstillinger for en bestemt kilde kan gives under `sources: {<navn>: {...}}`. »Tag kontrol« i HA gør dashboardet til almindelig konfiguration, man selv kan rette i.

**Luft-luft-styring (stigen):** har en luft-luft-kilde en klimaentitet – valgt i opsætningen eller fundet på samme enhed som enhedens føler – får den sine egne indstillinger, der vises som dropdowns i kildens fane:

| Entitet | Hvad |
|---|---|
| *Jeg fryser* (kontakt) | øverste trin (powerful, hvis varmepumpen har det) – også i blokerede timer – i højst »Jeg fryser gælder højst« timer; slår selv fra |
| *Varmemodel styrer* (kontakt) | fra = kun anbefaling (standard). Til = Varmemodel skriver tænd/sluk, blæser og setpunkt til klimaentiteten (højst 60 skrivninger pr. døgn). Slå egne automationer for varmepumpen fra først. |
| *Prioritet* (valg) | økonomi – balance – komfort: hvor hårdt stigen går til, når et rum er under gulvet (se ovenfor) |
| *Min. blæser* (valg, 0.29.0) | den laveste blæser, stigen må bruge – fx når et fjernt rum ikke får del i varmen ved lav blæser. Trinene under den (og *quiet*) udgår; standard er den laveste (ingen grænse). Ligger den over *Maks. blæser* (fx om natten), vinder maks., og stigen er kun setpunkter |
| *Maks. blæser dag / nat* (valg) | hvor meget den må larme – hastighederne læses fra klimaentiteten |
| *Nat begynder / slutter* (tid) | standard 22:00–07:00 |
| *Lameller op/ned og side* (valg) | »uændret« eller en fast stilling |

**Forbrug pr. trin (0.12.1):** vælg luft-luft-kildens *Eleffekt* (W) i opsætningen – energimålere er for grove pr. kvarter. Trinene navngives relativt til laveste setpunkt (fx »High +1,5«), så læringen overlever, at gulvene og dermed setpunkterne flytter sig. Første gang fyldes forbruget pr. trin ud fra de seneste 3 døgns historik (det, varmepumpen faktisk har kørt).

**Mest varme pr. kWh el uden ekstra følere:** for hvert trin lærer Varmemodel, hvor meget rummene stiger pr. kWh el – målt ændring minus det, rummet ville have gjort uden varmepumpen (afkøling mod ude, gratis varme som opdrift fra etagen under, naborum). Det læres kun, når trinnet har kørt i mindst 45 min. Når to eller flere trin er lært, vælger stigen det trin, der dækker planens varme for mindst el – og svarer dermed selv på, om lav blæser i mange timer eller høj i få er billigst. Kortet viser virkningen pr. trin i % af det bedste.

**Trinene deler, hvad de lærer (0.29.0):** nat og dag har hver sin maks. blæser og dermed hver sine trin (»HighMid +0,5« og »High +0,5«), men samme setpunkt over gulvet. Et trin uden egne målinger bruger de andre blæseres tal for samme setpunkt – forbrug og virkning lægges sammen på tværs af blæsere (i udetemperaturens bånd, ellers alle; virkningen til sidst også uanset ventilation) – og mangler også de, et skøn mellem de nærmeste lærte setpunkter. Egne målinger går altid forud, og kun stigens egne blæsere deles (ikke fx »Auto« fra en anden styring). Valget af det trin, der giver mest varme pr. kWh el, bruger kun egne og delte tal – et skøn er for løst at vælge mellem trin på; skønnet bruges til at finde det laveste trin, hvis forbrug dækker planens el, og til visningen. Det samme gælder starttrinnet: er det lært med en anden maks. blæser, starter stigen på samme setpunkt over gulvet. Trin-føleren viser kilden for hvert tal (`w_kilde`, `virkning_kilde`: `trin`, `delt` eller `skøn`), og kortet mærker dem »(delt)« og »(skøn)«. Føleren har også `rum_pr_blæser`: hvert rums varme pr. kWh el for hver blæser (samles fra 0.29.0) – grundlaget for at se, om et fjernt rum får del i varmen ved lav blæser, og senere for at lære *Min. blæser*.

**Ventilation der fordeler varmen:** vælg den (og evt. dens hastighed) i luft-luft-kildens opsætning. Så læres virkningen pr. trin for ventilation til og fra hver for sig, og kortet viser, hvor meget mere varme rummene får pr. kWh el, når den kører – en løbende A/B-test.

Resten regner modellen selv: setpunkterne flyder fra rummenes højeste gulv og 3 °C op (inden for klimaentitetens grænser), trinnet følger planens el-mål for timen (planen har valgt timer og mængde efter pris), og der gås kun ned, når alle rum er over gulv + forvarmning + rummodellens egen usikkerhed.

Stigen: først blæseren op (fra *Min. blæser*; laveste trin er varmepumpens *quiet*, hvis den har det, og min. blæser ikke er sat højere), så setpunktet op – blæseren højst til den valgte maks. Når planen siger varme, går stigen ét trin op pr. kvarter (komfort; økonomi og balance: pr. halve time, se *Prioritet*), mens et rum er under sit gulv (+ forvarmning før en blokering, regnet som planens forventede fald i rummet), og ét trin ned, når alle rum har været over gulvet + margen i 30 min., og trinnet har holdt i 45 min. Starttrinnet huskes pr. 5 °C udetemperatur, og forbruget pr. trin læres af elmåleren ud fra det, der faktisk kører – også mens Varmemodel kun anbefaler. Sensoren *Trin på stigen* viser trinene, det lærte forbrug, margen og forvarmning pr. rum.

**Luft-luft med plantegning:** med `source` sat til en luft-luft-kilde og `floor` (samme format som én etage i plantegningskortet) vises etagen som en »bento-plan«: rummene som kort placeret som på tegningen (rækker efter indhold, ikke cm). Hvert kort viser temperaturen, en kurve med de sidste 12 timer og forudsigelsen 6 timer frem (stiplet), gulvet (tryk for at rette) og retningen om 3 timer. Kortets farve glider fra blå (under gulvet) over grøn til rød (over loftet). Varmepumpen står som et mærke i sit rum og drejer, mens den varmer. På smal skærm bliver kortene til to kolonner. Til venstre står varmepumpen med valgfrie `stats`, kWh el pr. time (24 timer tilbage farvet efter pris, planen 12 timer frem stiplet) og pr. døgn, og valgfrit `steps`: hvad hvert trin gør og dets lærte forbrug.

```yaml
type: custom:varmemodel-anlaeg-card
source: Air to air
floor: {name: 1st floor, width: 700, height: 500, rooms: [...], units: [...]}
stats:
  - {name: Control, entity: input_select.control}
  - {name: Defrosts today, entity: sensor.defrosts_today}
steps:
  entity: input_number.step          # trin nu
  learned: sensor.watt_per_step      # lært forbrug pr. trin i en attribut
  learned_attribute: w               # {"0": 180, "1": 260, ...} i W
  labels: [quiet 20 °C, Low 20 °C, Mid 20 °C, High 20 °C]
sources:
  Air to air:
    climate: climate.air_to_air
    el_power: sensor.air_to_air_power
    defrost: binary_sensor.air_to_air_defrost   # valgfri
```

Live COP vises kun i stabil drift (kompressoren har kørt i 10 min., ingen afrimning eller varmt vand) og sammenlignes med den forventede fra *COP i stabil drift*. Er den mere end 0,5 under i 30 min., markeres det.

**Plantegningskortet** virker uden opsætning: så vises rummene som farvede felter pr. varmekilde. Med en plantegning tegner det rummene efter din tegning og farver dem efter temperaturen i forhold til rummets gulv og loft: blå under gulvet, lyseblå tæt på, grøn godt, gul tæt på loftet og rød over. Knapperne viser forudsigelsen om 3, 6 og 12 timer, og et tryk på et rum åbner føleren. Koordinaterne er i cm og kan tages fra en salgstegning. Med `source` vises varmekildens plan time for time og den anbefalede styring under tegningen. Det gør kortet til et anlægskort for netop den varmepumpe.

```yaml
type: custom:varmemodel-plan-card
title: 1. sal
source: Luft-luft          # valgfri: varmekildens plan og anbefalede styring
hours: [0, 3, 6, 12]
floors:
  - name: 1. sal
    width: 720             # cm
    height: 900
    rooms:
      - name: Værelse 1    # rummets navn i Varmemodel
        rect: [0, 0, 280, 325]          # x, y, bredde, højde
      - name: Gang
        points: [[280, 0], [420, 0], [420, 520], [280, 520]]
      - room: Gang         # samme rum, kun farve (fx trappeopgang)
        rect: [0, 325, 280, 105]
      - label: Teknik      # ikke et rum: gråt
        rect: [145, 520, 430, 150]
    doors:
      - [280, 235, 280, 275]            # åbning: x1, y1, x2, y2
    units:
      - name: Varmepumpe
        x: 25
        y: 378
        to: [[330, 378], [330, 470]]    # luftens vej (pil); flere veje med paths
```

Har du en tegning som billede (fx fra en salgsopstilling), så læg den i `/config/www` og angiv `image: /local/stueplan.png` på etagen. Brug billedets mål (cm) som `width` og `height`, og tegn rummene som simple firkanter ovenpå (`room_opacity` styrer gennemsigtigheden). En føler kan placeres med `sensor: [x, y]` på rummet.

**Rumkortet** grupperer efter varmekilde eller efter dine egne grupper:

```yaml
type: custom:varmemodel-rum-card
compact: true
groups:
  - name: Stueplan
    rooms: [Stue, Køkken]
```

## Sådan læser du tallene

Alle tal er **målt i dit eget hus**. De bliver bedre, jo længere integrationen har kørt, og hvert tal fortæller selv, hvor sikkert det er.

### Hvornår kan man stole på et tal?

Hver sensor har attributter, der viser datagrundlaget:

| Attribut | Betydning | Tommelfingerregel |
|---|---|---|
| `målinger` / `blokke` | hvor mange timer, portioner eller blokke tallet bygger på | under ca. 50 = foreløbigt |
| `forklaringsgrad` (R²) | hvor stor en del af udsvingene modellen forklarer (0–1) | over 0,6 = godt, under 0,3 = usikkert |
| `præcision_6_timer` | rummets typiske fejl, når det forudsiges 6 timer frem (°C) | under ±0,3 °C = godt |
| `præcision_6_timer_afprøvet` | samme, men afprøvet ærligt: modellen køres 6 timer frem fra den målte temperatur med den målte tilførsel, hver 3. time i hele perioden, uden at sortere afvigere fra. Den største af de to bruges som rummets usikkerhed i planen og tjekket. Bremser ±10 %-grænsen tallet, beholdes den forrige model, og det er den, der afprøves | tæt på `præcision_6_timer` = modellen holder; meget større = fittet er for optimistisk |
| `skævhed_6_timer_afprøvet` | middelfejlen i afprøvningen (målt − ventet) | nær 0 = ingen skævhed; plus = rummet bliver varmere end ventet |
| `ude_fra` / `ude_til` | udetemperatur-området tallet er målt i | gælder ikke uden for området – fx før første frost |
| `begrænsning` | om ±10 %-grænsen har bremset tallet i nat | »begrænset« = tallet er på vej mod en ny værdi |
| `kilde` | `fit` = beregnet i nat, `beholdt` = sidste gode værdi | |
| `note` | hvorfor et tal mangler, fx »for få blokke endnu« | |

### Varmekilde: luft-vand

| Sensor | Enhed | Sådan læses den | Typisk |
|---|---|---|---|
| **Varmetab** | W/K | Husets varmetab pr. grad mellem inde og ude. 100 W/K betyder 2,4 kWh varme i døgnet pr. grad. **Lavere er bedre.** Falder tallet efter fx efterisolering, virkede det. Attributten `gains_w` er gratis varme (mennesker, el-apparater, sol) i W. | 60–250 W/K |
| **COP-reference A0/W35 4 kW (stabil drift)** | – | Varme ÷ el, når varmepumpen kører stabilt, regnet ud ved referencepunktet 0 °C ude, 35 °C fremløb og 4 kW, så tallet kan sammenlignes over tid. Attributterne `a`, `b`, `c`, `d` er formlen: COP = a + b·ude + c·fremløb + d·effekt. `per_k_flow_pct` = hvor mange % COP stiger pr. grad lavere fremløb. `c_source` = `prior`: fremløbets virkning er endnu ikke målbar, og en standardværdi bruges. | 2–4 |
| **Ekstra el ved opstart, afrimning og standby** | % | Andelen af el ud over, hvad stabil drift ville have brugt til samme varme (målt mod målt, samme periode). Stiger typisk i frostvejr. | 0–15 % |
| **COP i dag** | – | kWh varme ÷ kWh el siden midnat, opdateret hvert kvarter. Attributterne: varmt vand, COP uden varmt vand, stabil drift, ekstra el i %, og hvad modellen forventede. | 2–5 |
| **COP i går** / **seneste 7 døgn** | – | Det rå regnestykke: kWh varme ÷ kWh el. Attributterne viser COP uden varmt vand og `forventet_stabil_cop`. `forskel_fra_stabil_pct` = hvor meget dårligere dagen var end stabil drift. `seneste_14_døgn` er en liste til grafer. | 2–5 |
| **Gulvets afgivelse** | kW/K | Hvor mange kW gulvet afgiver pr. grad fremløb over rumtemperaturen. Højt = gulvet kan klare sig med lavt fremløb. | 0,1–0,5 |
| **Maks. effekt** / **Laveste målte effekt** | kW | Største og mindste varmeeffekt, varmepumpen har leveret i fulde driftstimer. Den laveste viser, hvor lavt den kan modulere, før den begynder at starte og stoppe. | |
| **Lært fremløb ved 0 °C ude** | °C | Det fremløb, huset har brugt ved ren rumvarme (kompressoren hele timen, ikke varmt vand), ved 0 °C ude. `slope` = ændring pr. grad ude (negativ). Bruges som loft i »lavt og jævnt«. | 30–40 °C |
| **Afrimninger pr. kompressortime** | – | Gennemsnit. `per_bin` viser det fordelt på udetemperatur og høj/lav effekt – lav effekt afrimer typisk sjældnere. | 0–1,5 |
| **Afkølingstid** | timer | Husets tidskonstant: hvor længe det tager at tabe varmen uden opvarmning. Lang = tungt hus med stor varmekapacitet. | 50–500 t |
| **Vind: ekstra varmetab pr. m/s** | % | Hvor mange % varmetabet stiger pr. m/s vind. `sun_kw_clear` = solens gratis varme ved klar himmel. `sectors` = afkøling pr. vindretning – hvilken side af huset er mest utæt. Kræver ca. 30 dages vejrdata. | 1–5 %/(m/s) |

### Varmekilde: luft-luft

| Sensor | Enhed | Sådan læses den |
|---|---|---|
| **El i går** / **El pr. døgn, 7 døgn** | kWh | Elforbruget. `supplerende_måler_faktor` = hvor meget den supplerende måler skaleres for at ramme hovedmåleren (fx 1,3 = skyen måler 30 % for lavt). |

Varmen fra en luft-luft-varmepumpe kan ikke måles direkte. Rummene lærer i stedet, hvor mange grader de får pr. kWh el (se *Varmes af* herunder).

### Rum

| Sensor / attribut | Enhed | Sådan læses den |
|---|---|---|
| **Afkøling** | °C/t | Hvor mange grader rummet falder i timen uden varme ved 20 graders forskel til ude. **Lavere er bedre.** Tallet falder, når rummet isoleres eller tætnes. Det er tallet, du skal se på før og efter et energitiltag. |
| `varmekilder_grader_pr_kwh` | °C/kWh | Hvor mange grader rummet stiger pr. kWh fra hver varmekilde (luft-luft: pr. kWh el). Højt = kilden varmer rummet godt. `varmekilder_detaljer` deler det op i *nu* og *forsinket* (gulvvarme afgiver varmen over flere timer). |
| `nabozoner` | °C/t ved 5 °C forskel | Hvor meget en nabozone trækker rummet mod sin temperatur. Fx 0,10 = er kælderen 5 °C varmere, stiger rummet 0,1 °C i timen af den grund. |
| `gratis_varme_grader_pr_døgn` | °C/døgn | Varme fra mennesker, madlavning og el-apparater. |
| `sol_grader_pr_klar_time` | °C | Solens bidrag i en klar time. |
| `tidskonstant_timer` | timer | Rummets egen afkølingstid. |
| `udeladt_udluftning` | blokke | Blokke uden for modellen pga. brat fald (udluftning, døre). |
| **Varme** | kWh/døgn | Rummets del af den vandbårne varme (snit af 7 døgn), fordelt efter telestatens åbningstid × kredsstørrelse. **Kun så præcis som kredsstørrelserne** – angiv flow eller længde. `andel_af_gulvvarmen_pct` er rummets andel, `brat_afkøling_pr_døgn` er døre og udluftning. |

### Eksempler

- **Efterisolering af et rum:** Notér rummets *Afkøling* før. Efter 2–3 uger skal tallet være lavere ved samme `forklaringsgrad`.
- **En dør der står åben:** Høj `brat_afkøling_pr_døgn` i et rum peger på en dør eller vane.
- **Lavere fremløb:** *COP i stabil drift* `per_k_flow_pct` viser gevinsten pr. grad.
- **En varm kælder:** En nabozone med stor værdi betyder, at varmen går mellem rummene.

## Diagnostik

Diagnostikfilen indeholder kalibreringen og de brugte timer med tid som "timer før seneste time". Den indeholder ingen entitets-id'er.

**Genkør en plan (0.23.0):** diagnostikken har også den seneste plans input for hver vandbåren kilde (`plan_input`): timerne som "timer fra nu" med pris, udetemperatur og blokering, kildens model, rummene som `rum_1`, `rum_2` … og planens indstillinger. Planen kan genkøres uden for huset:

```
python tools/replay_plan.py diagnostik.json
python tools/replay_plan.py diagnostik.json --sæt flow_raise_max=0
```

Den første linje viser varme, el, pris, største manko og hævet fremløb. Med `--sæt` kan enkelte argumenter ændres, så en rettelse kan afprøves mod en rigtig situation, før den installeres.

## Planer

- **0.4–0.5 (her):** Planlæggeren for vandbårne varmekilder, indstillinger, log, rumkontrol og før/efter. Senere: luft-luft i planen.
- **0.5:** Gulvkomfort, sikkerhedsnet og en blueprint til at udføre planen.
