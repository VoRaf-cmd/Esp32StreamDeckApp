#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

Adafruit_SSD1306 display(128, 64, &Wire, -1);

// 0-3 = ações, 4 = página anterior, 5 = próxima página
const uint8_t PINS[] = {13, 14, 25, 26, 27, 32};
const uint8_t N_PINS = 6;
const uint8_t N_PAGES = 3;
const uint8_t POT_PIN = 34;

bool lastState[N_PINS];
uint32_t lastChange[N_PINS];
uint8_t page = 0;
int lastBtn = -1;

float potSmooth = 0;
int potPercent = 0;
int lastSentPot = -1;
uint32_t lastPotRead = 0;

void draw() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Linha superior: página e valor do potenciômetro
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.printf("Pagina %d/%d", page + 1, N_PAGES);
  display.setCursor(86, 0);
  display.printf("Pot %d%%", potPercent);

  // Centro: último botão acionado
  display.setTextSize(2);
  display.setCursor(0, 22);
  if (lastBtn < 0) display.print("Pronto");
  else display.printf("Botao %d", lastBtn + 1);

  // Barra do potenciômetro
  display.drawRect(0, 52, 128, 12, SSD1306_WHITE);
  display.fillRect(2, 54, (124 * potPercent) / 100, 8, SSD1306_WHITE);

  display.display();
}

void onPress(uint8_t i) {
  if (i == 4) page = (page + N_PAGES - 1) % N_PAGES;
  else if (i == 5) page = (page + 1) % N_PAGES;
  else {
    lastBtn = i;
    Serial.printf("B,%d,%d\n", page, i);  // B,pagina,botao
  }
  draw();
}

void readPot() {
  // Média móvel para reduzir oscilação
  potSmooth = potSmooth * 0.8f + analogRead(POT_PIN) * 0.2f;
  potPercent = constrain(map((int)potSmooth, 0, 4095, 0, 100), 0, 100);

  bool changed = abs(potPercent - lastSentPot) >= 2;
  bool reachedEdge = potPercent != lastSentPot && (potPercent == 0 || potPercent == 100);
  if (changed || reachedEdge) {
    lastSentPot = potPercent;
    Serial.printf("P,%d\n", potPercent);  // P,valor (0 a 100)
    draw();
  }
}

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < N_PINS; i++) {
    pinMode(PINS[i], INPUT_PULLUP);
    lastState[i] = true;
  }
  potSmooth = analogRead(POT_PIN);

  Wire.begin(21, 22);
  display.begin(SSD1306_SWITCHCAPVCC, 0x3C);  // se ficar em branco, tente 0x3D
  readPot();
  draw();
}

void loop() {
  // Botões com debounce
  for (uint8_t i = 0; i < N_PINS; i++) {
    bool s = digitalRead(PINS[i]);
    if (s != lastState[i] && millis() - lastChange[i] > 30) {
      lastChange[i] = millis();
      lastState[i] = s;
      if (!s) onPress(i);
    }
  }

  // Potenciômetro a cada 20 ms
  if (millis() - lastPotRead > 20) {
    lastPotRead = millis();
    readPot();
  }
}
