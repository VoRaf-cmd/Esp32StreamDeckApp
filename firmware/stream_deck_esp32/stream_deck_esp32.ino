#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

Adafruit_SSD1306 display(128, 64, &Wire, -1);

// 0-3 = ações, 4 = página anterior, 5 = próxima página
const uint8_t PINS[] = {13, 14, 25, 26, 27, 32};
const uint8_t N_PINS = 6;
const uint8_t MAX_PAGES = 10;
uint8_t pageCount = 3;
const uint8_t N_BTN = 4;
const uint8_t POT_PIN = 34;
const uint8_t LABEL_LEN = 21;      // 20 caracteres + terminador
const uint16_t FLASH_MS = 700;     // tempo da tela grande ao apertar

char labels[MAX_PAGES][N_BTN][LABEL_LEN];  // nomes enviados pelo app do PC
bool lastState[N_PINS];
uint32_t lastChange[N_PINS];
uint8_t page = 0;
int8_t pressedBtn = -1;
uint32_t flashUntil = 0;

float potSmooth = 0;
int potPercent = 0;
int lastSentPot = -1;
uint32_t lastPotRead = 0;

char rx[48];
uint8_t rxLen = 0;
bool labelsDirty = false;

// Desenha um quadradinho de botão (invertido = apertado)
void drawCell(uint8_t i, bool inverted) {
  int x = (i % 2) * 65;
  int y = 12 + (i / 2) * 20;
  if (inverted) display.fillRoundRect(x, y, 63, 18, 3, SSD1306_WHITE);
  else display.drawRoundRect(x, y, 63, 18, 3, SSD1306_WHITE);
  display.setTextColor(inverted ? SSD1306_BLACK : SSD1306_WHITE);
  display.setTextSize(1);
  char buf[11];
  const char* t = labels[page][i];
  strncpy(buf, t[0] ? t : "-", 10);
  buf[10] = 0;
  int w = strlen(buf) * 6;
  display.setCursor(x + (63 - w) / 2, y + 5);
  display.print(buf);
}

// Tela normal: página, volume, 4 botões com nome e barra do potenciômetro
void drawGrid() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 2);
  display.printf("Pagina %d/%d", page + 1, pageCount);
  display.setCursor(80, 2);
  display.printf("Vol %3d%%", potPercent);
  for (uint8_t i = 0; i < N_BTN; i++) drawCell(i, false);
  display.drawRect(0, 54, 128, 8, SSD1306_WHITE);
  display.fillRect(2, 56, (124 * potPercent) / 100, 4, SSD1306_WHITE);
  display.display();
}

// Tela grande com o nome da ação apertada
void drawFlash() {
  display.clearDisplay();
  display.fillRoundRect(0, 0, 128, 64, 6, SSD1306_WHITE);
  display.setTextColor(SSD1306_BLACK);

  char fallback[12];
  const char* t = labels[page][pressedBtn];
  if (!t[0]) {
    snprintf(fallback, sizeof(fallback), "Botao %d", pressedBtn + 1);
    t = fallback;
  }
  uint8_t len = strlen(t);
  uint8_t size = (len <= 10) ? 2 : 1;
  int w = len * 6 * size;
  int x = (128 - w) / 2;
  if (x < 0) x = 0;
  display.setTextSize(size);
  display.setCursor(x, (size == 2) ? 26 : 30);
  display.print(t);

  display.setTextSize(1);
  display.setCursor(6, 6);
  display.printf("P%d  B%d", page + 1, pressedBtn + 1);
  display.display();
}

void draw() {
  if (flashUntil) drawFlash();
  else drawGrid();
}

void onPress(uint8_t i) {
  if (i == 4) {
    page = (page + pageCount - 1) % pageCount;
    flashUntil = 0;
  } else if (i == 5) {
    page = (page + 1) % pageCount;
    flashUntil = 0;
  } else {
    pressedBtn = i;
    flashUntil = millis() + FLASH_MS;
    Serial.printf("B,%d,%d\n", page, i);  // B,pagina,botao
  }
  draw();
}

void readPot() {
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

// Recebe linhas do PC no formato: L,pagina,botao,texto
void handleLine(char* s) {
  if (s[0] == 'C' && s[1] == ',') {
    int count = atoi(s + 2);
    if (count >= 1 && count <= MAX_PAGES) {
      pageCount = count;
      if (page >= pageCount) page = pageCount - 1;
      labelsDirty = true;
    }
    return;
  }
  if (s[0] != 'L' || s[1] != ',') return;
  char* p1 = s + 2;
  char* c1 = strchr(p1, ',');
  if (!c1) return;
  *c1 = 0;
  char* b1 = c1 + 1;
  char* c2 = strchr(b1, ',');
  if (!c2) return;
  *c2 = 0;
  int p = atoi(p1);
  int b = atoi(b1);
  if (p < 0 || p >= pageCount || b < 0 || b >= N_BTN) return;
  strncpy(labels[p][b], c2 + 1, LABEL_LEN - 1);
  labels[p][b][LABEL_LEN - 1] = 0;
  labelsDirty = true;
}

void readSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      rx[rxLen] = 0;
      if (rxLen && rx[rxLen - 1] == '\r') rx[rxLen - 1] = 0;
      handleLine(rx);
      rxLen = 0;
    } else if (rxLen < sizeof(rx) - 1) {
      rx[rxLen++] = c;
    }
  }
  if (labelsDirty) {
    labelsDirty = false;
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
  readSerial();

  for (uint8_t i = 0; i < N_PINS; i++) {
    bool s = digitalRead(PINS[i]);
    if (s != lastState[i] && millis() - lastChange[i] > 30) {
      lastChange[i] = millis();
      lastState[i] = s;
      if (!s) onPress(i);
    }
  }

  if (millis() - lastPotRead > 20) {
    lastPotRead = millis();
    readPot();
  }

  if (flashUntil && (int32_t)(millis() - flashUntil) >= 0) {
    flashUntil = 0;
    draw();
  }
}
