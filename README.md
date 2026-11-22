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
| Supplerende elmåler (fx skyen) | valgfrit | valgfrit – bruges hvor hovedmåleren mangler data, skaleret til hovedmålerens niveau |
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
3. **Rummene forudsiges** time for time med rummodellerne (varme nu og forsinket, afkøling, gratis varme, nabozoner). Kommer et rum under min. + sin usikkerhed, lægges der varme i den billigste time før.
4. For lange pauser brydes med den billigste time.
5. **Fremløb** = mål + effekt ÷ gulvets afgivelse, og **kurve-forskydning** = fremløb − varmekurven.

**Indstillinger (tal-entiteter på huset):** *Maks. pause uden gulvvarme*, *Komfort over min.*, *»Jeg fryser« hæver målet*, *»Jeg fryser« gælder højst* (timer) og *Må falde under min. i blokerede timer*. De påvirker kun planen.

**»Jeg fryser«** gælder fra den slås til, i højst det valgte antal timer og kun til næste blokerede periode begynder. Der varmes aldrig i blokerede timer.

**Log og kontrol:**
- Ved hver times start gemmes, hvad planen sagde for timen.
- Næste nat sammenlignes gårsdagen: planlagt mod faktisk varme, el, kroner og varme i blokerede timer (attributten *i_går* på *Plan pris*).
- Hvert rums forudsigelse fra 6 timer før sammenlignes med den målte temperatur. Afviger den mere end 2 × rummets præcision (mindst 0,3 °C), markeres rummet (attributten *rooms_check* på *Plan nu*), fx ved en åben dør.

**Anbefalet styring (sensor pr. vandbåren kilde):** viser, hvad en automation bør sende nu, efter regler der skåner varmepumpen. Den styrer ikke selv noget.

Reglerne:
- aldrig varme i blokerede timer; slukker straks
- mindst 60 min. tændt og 30 min. slukket
- højst én ændring pr. 15 min.
- kurve-forskydningen holdes mindst 1 time og ændres med mindst 1 K

Attributterne tæller pr. døgn skrivninger, tænd/sluk-skift og varme i blokerede timer. *within_limits* er sand, når der er højst 20 skrivninger, højst 8 skift og ingen varme i blokerede timer.

**Nødplan:** mangler elprisen helt, planlægges med flad pris, så rummene stadig holdes over komfortgulvet. Det står i begrundelsen, og *inputs_ok* er falsk.

**Følere der falder ud:** svarer en rum- eller udeføler ikke, bruges den sidst kendte værdi i højst 2 timer. Det sker fx, når en skytjeneste er nede.
- Rummet er stadig med i planen og står under *rooms_held* på *Plan nu*.
- Imens logges og tjekkes rummets forudsigelse ikke, fordi startpunktet ikke er målt.
- Efter 2 timer udelades rummet og står under *rooms_skipped*. Begge dele vises som afvigelse i kortene.

**Huller og sommertid:**
- Planen regner i UTC-timer.
- Mangler prisen for én enkelt time, bruges snittet af nabotimerne. Det sker fx, når uret stilles tilbage, og to lokale timer hedder det samme.
- Nye priser, typisk kl. 13, kommer med ved næste planlægning, senest efter 15 min.

**Frisk plan:** *fresh_pct_today* på *Plan nu* er den andel af dagens kvarterer, der havde en frisk plan med alle input.

**Usikkerhed:** *rooms_band* på *Plan nu* er ± usikkerheden for hvert rums forudsigelse.

**Energitiltag:** Vælg en kalender under *Konfigurer*. Hver begivenhed er et tiltag. Ved kalibreringen regnes varmetabet for sig før og efter datoen (dagen selv udelades). Det kræver ca. 16 døgn på hver side. Resultatet ses på sensoren *Energitiltag*.

**Sensorer pr. varmekilde:** *Plan nu* (varme / slukket / blokeret, med effekt, fremløb, forskydning, pris, grund, næste start/stop, hele planen og rummenes forudsigelse i attributterne), *Plan fremløb*, *Plan kurve-forskydning* og *Plan pris* (forventet elpris for perioden).

Til planlægning bruges COP-formlen fra *COP i stabil drift*, men højere effekt eller fremløb må aldrig gøre COP bedre: et positivt effektled holdes fast ved referenceeffekten (det skyldes typisk koldt gulv efter pauser, ikke effekten selv).

## Kort til dashboardet

Integrationen leverer fem kort, som selv finder Varmemodels entiteter (Hus → varmekilde → rum). Tilføj dem med *Tilføj kort* eller i YAML:

| Kort | Viser |
|---|---|
| `custom:varmemodel-anlaeg-card` | anlægget nu: varmekilde, kredsløb, rum, planen på en tidslinje og afvigelser |
| `custom:varmemodel-rum-card` | rummene: målt, forudsagt for 6 timer siden (ramt, over eller under), de næste timer og rummodellens tal |
| `custom:varmemodel-kilde-card` | varmekildernes tal: effektivitet, COP for 14 døgn, bygning og anlæg |
| `custom:varmemodel-tiltag-card` | energitiltag: hvilke rum taber varmen hurtigst, vaner, vind og før/efter |
| `custom:varmemodel-hus-card` | status, planen og planens indstillinger |

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
    pump: input_select.pump_curve       # cirkulationspumpe efter kortslutningen
    pump_running: binary_sensor.pump_running
    a: sensor.pipe_heat_pump_out        # følere: A varmepumpe ud, B manifold ind,
    b: sensor.pipe_manifold_in          #         C manifold retur, D varmepumpe ind
    c: sensor.pipe_manifold_return
    d: sensor.pipe_heat_pump_in
  Air to air:
    climate: climate.air_to_air
    el_power: sensor.air_to_air_power
```

Live COP vises kun i stabil drift (kompressoren har kørt i 10 min., ingen afrimning eller varmt vand) og sammenlignes med den forventede fra *COP i stabil drift*. Er den mere end 0,5 under i 30 min., markeres det.

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
| `ude_fra` / `ude_til` | udetemperatur-området tallet er målt i | gælder ikke uden for området – fx før første frost |
| `begrænsning` | om ±10 %-grænsen har bremset tallet i nat | »begrænset« = tallet er på vej mod en ny værdi |
| `kilde` | `fit` = beregnet i nat, `beholdt` = sidste gode værdi | |
| `note` | hvorfor et tal mangler, fx »for få blokke endnu« | |

### Varmekilde: luft-vand

| Sensor | Enhed | Sådan læses den | Typisk |
|---|---|---|---|
| **Varmetab** | W/K | Husets varmetab pr. grad mellem inde og ude. 100 W/K betyder 2,4 kWh varme i døgnet pr. grad. **Lavere er bedre.** Falder tallet efter fx efterisolering, virkede det. Attributten `gains_w` er gratis varme (mennesker, el-apparater, sol) i W. | 60–250 W/K |
| **COP i stabil drift** | – | Varme ÷ el, når varmepumpen kører stabilt – ved 0 °C ude, 35 °C fremløb og 4 kW, så tallet kan sammenlignes over tid. Attributterne `a`, `b`, `c`, `d` er formlen: COP = a + b·ude + c·fremløb + d·effekt. `per_k_flow_pct` = hvor mange % COP stiger pr. grad lavere fremløb. `c_source` = `prior`: fremløbets virkning er endnu ikke målbar, og en standardværdi bruges. | 2–4 |
| **Effektiv COP** | – | Som ovenfor, men over hele timer inkl. opstart og afrimning. | lidt under stabil |
| **Tab ved opstart og afrimning** | % | Hvor meget lavere den faktiske COP er end i stabil drift. Stiger typisk i frostvejr. Negativt = støj. | 0–15 % |
| **COP i går** / **seneste 7 døgn** | – | Det rå regnestykke: kWh varme ÷ kWh el. Attributterne viser COP uden varmt vand og `forventet_stabil_cop`. `forskel_fra_stabil_pct` = hvor meget dårligere dagen var end stabil drift. `seneste_14_døgn` er en liste til grafer. | 2–5 |
| **Gulvets afgivelse** | kW/K | Hvor mange kW gulvet afgiver pr. grad fremløb over rumtemperaturen. Højt = gulvet kan klare sig med lavt fremløb. | 0,1–0,5 |
| **Maks. effekt** / **Laveste målte effekt** | kW | Største og mindste varmeeffekt, varmepumpen har leveret i fulde driftstimer. Den laveste viser, hvor lavt den kan modulere, før den begynder at starte og stoppe. | |
| **Varmekurve ved 0 °C** | °C | Fremløbet ved 0 °C ude uden kurve-forskydning. `slope` = ændring pr. grad ude (negativ). | 30–40 °C |
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

## Planer

- **0.4–0.5 (her):** Planlæggeren for vandbårne varmekilder, indstillinger, log, rumkontrol og før/efter. Senere: luft-luft i planen.
- **0.5:** Gulvkomfort, sikkerhedsnet og en blueprint til at udføre planen.
