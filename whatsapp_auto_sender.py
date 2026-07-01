import os
import time
import urllib.parse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


CHROME_BINARY_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Users/evon/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

OPENCLAW_CHROME_PROFILE = "/Users/evon/OpenClaw/chrome_whatsapp_profile"
DEBUG_SCREENSHOT = "/Users/evon/OpenClaw/whatsapp_debug.png"


def find_chrome_binary():
    for path in CHROME_BINARY_PATHS:
        if os.path.exists(path):
            print(f"✅ Chrome binary found: {path}")
            return path

    raise FileNotFoundError("Google Chrome binary not found.")


def init_driver():
    chrome_binary = find_chrome_binary()

    os.makedirs(OPENCLAW_CHROME_PROFILE, exist_ok=True)

    options = Options()
    options.binary_location = chrome_binary

    options.add_argument(f"--user-data-dir={OPENCLAW_CHROME_PROFILE}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    return webdriver.Chrome(options=options)


def is_logged_in(driver):
    logged_in_selectors = [
        '//div[@contenteditable="true"][@role="textbox"]',
        '//div[@aria-label="Search input textbox"]',
        '//button[@aria-label="New chat"]',
        '//div[@id="side"]',
        '//header',
    ]

    for selector in logged_in_selectors:
        try:
            elements = driver.find_elements(By.XPATH, selector)
            if elements:
                return True
        except Exception:
            continue

    return False


def wait_for_login(driver, timeout=90):
    print("🟢 Waiting for WhatsApp Web login/session...")

    end_time = time.time() + timeout

    while time.time() < end_time:
        if is_logged_in(driver):
            print("✅ WhatsApp Web session detected.")
            return True

        print("   ⏳ Waiting for login/session...")
        time.sleep(3)

    print("❌ WhatsApp session not detected in time.")
    return False


def wait_for_chat_ready(driver, timeout=60):
    print("⌛ Waiting for chat/message box...")

    textbox_selectors = [
        '//footer//div[@contenteditable="true"]',
        '//div[@contenteditable="true"][@role="textbox"]',
        '(//div[@contenteditable="true"])[last()]',
    ]

    end_time = time.time() + timeout

    while time.time() < end_time:
        for selector in textbox_selectors:
            try:
                box = driver.find_element(By.XPATH, selector)
                if box.is_displayed():
                    print("✅ Message box detected.")
                    return box
            except Exception:
                continue

        time.sleep(2)

    return None


def click_send_button(driver):
    send_selectors = [
        '//button[@aria-label="Send"]',
        '//button[@aria-label="Send message"]',
        '//span[@data-icon="send"]/ancestor::button',
        '//span[@data-icon="send"]/ancestor::div[@role="button"]',
        '//*[@data-icon="send"]',
    ]

    for selector in send_selectors:
        try:
            button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, selector))
            )
            driver.execute_script("arguments[0].click();", button)
            print("✅ Message sent by clicking Send button.")
            return True
        except Exception:
            continue

    return False


def send_whatsapp(phone, message):
    print(f"\n📲 Sending WhatsApp to: {phone}")

    driver = None

    try:
        driver = init_driver()

        print("🌐 Opening WhatsApp Web home...")
        driver.get("https://web.whatsapp.com")

        if not wait_for_login(driver, timeout=90):
            driver.save_screenshot(DEBUG_SCREENSHOT)
            print(f"📸 Debug screenshot saved: {DEBUG_SCREENSHOT}")
            return False

        encoded_msg = urllib.parse.quote(message)
        url = f"https://web.whatsapp.com/send?phone={phone}&text={encoded_msg}"

        print("🌐 Opening target WhatsApp chat...")
        driver.get(url)

        box = wait_for_chat_ready(driver, timeout=90)

        if not box:
            driver.save_screenshot(DEBUG_SCREENSHOT)
            print("❌ Chat/message box not detected.")
            print(f"📸 Debug screenshot saved: {DEBUG_SCREENSHOT}")
            return False

        time.sleep(3)

        if click_send_button(driver):
            time.sleep(5)
            return True

        print("⚠️ Send button not found. Trying ENTER method...")

        try:
            box.click()
            time.sleep(1)
            box.send_keys(Keys.ENTER)
            print("✅ Message sent by pressing ENTER.")
            time.sleep(5)
            return True
        except Exception as e:
            print(f"❌ ENTER send failed: {e}")

        driver.save_screenshot(DEBUG_SCREENSHOT)
        print("❌ Could not auto-send.")
        print(f"📸 Debug screenshot saved: {DEBUG_SCREENSHOT}")
        return False

    except Exception as e:
        print(f"❌ WhatsApp send failed: {e}")

        if driver:
            try:
                driver.save_screenshot(DEBUG_SCREENSHOT)
                print(f"📸 Debug screenshot saved: {DEBUG_SCREENSHOT}")
            except Exception:
                pass

        return False

    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    phone = "60167027683"

    message = """Hi,

Please quote the following:

BURKERT ID: 199983
Qty: 5 PCS

Thanks
"""

    send_whatsapp(phone, message)