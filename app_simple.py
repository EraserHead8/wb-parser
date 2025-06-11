from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import requests
import json
import time
import os
from datetime import datetime
import re
import csv
from io import BytesIO
import zipfile
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib.units import inch
import openai
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

app = Flask(__name__)
CORS(app)

# OpenAI API настройки
openai.api_key = os.getenv('OPENAI_API_KEY')
if not openai.api_key:
    print("WARNING: OPENAI_API_KEY environment variable is not set")

# Папка для сохранения файлов
UPLOAD_FOLDER = 'parsed_files'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

class WildberriesParser:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        
    def get_seller_id(self, seller_url):
        """Извлечение ID продавца из URL"""
        # Примеры URL:
        # https://www.wildberries.ru/seller/12345
        # https://www.wildberries.ru/brands/12345
        # https://www.wildberries.ru/brands/brand-name/all
        
        # Пробуем найти числовой ID
        match = re.search(r'/seller/(\d+)', seller_url)
        if match:
            return match.group(1)
        
        match = re.search(r'/brands/(\d+)', seller_url)
        if match:
            return match.group(1)
        
        # Если это текстовый бренд, нужно получить ID через поиск
        match = re.search(r'/brands/([^/]+)', seller_url)
        if match:
            brand_name = match.group(1)
            return self.get_brand_id(brand_name)
        
        raise ValueError("Не удалось извлечь ID продавца из URL")
    
    def get_brand_id(self, brand_name):
        """Получение ID бренда по имени"""
        search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?query={brand_name}"
        
        try:
            response = requests.get(search_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            if data.get('data') and data['data'].get('products') and len(data['data']['products']) > 0:
                # Берем supplierId первого товара
                supplier_id = data['data']['products'][0].get('supplierId')
                if supplier_id:
                    return str(supplier_id)
        except Exception as e:
            print(f"Ошибка при получении ID бренда: {e}")
        
        raise ValueError(f"Не удалось найти бренд: {brand_name}")
    
    def parse_seller_products(self, seller_url):
        """Парсинг всех товаров продавца"""
        print(f"\n{'='*50}")
        print(f"НАЧАЛО ПАРСИНГА ПРОДАВЦА")
        print(f"URL: {seller_url}")
        print(f"{'='*50}\n")
        
        try:
            seller_id = self.get_seller_id(seller_url)
            print(f"✓ ID продавца успешно извлечен: {seller_id}")
        except Exception as e:
            error_msg = f"✗ ОШИБКА извлечения ID продавца: {e}"
            print(error_msg)
            raise ValueError(error_msg)
            
        products = []
        page = 1
        total_requests = 0
        consecutive_errors = 0
        max_consecutive_errors = 3
        
        # Статистика для отладки
        stats = {
            'total_pages': 0,
            'total_requests': 0,
            'errors': [],
            'empty_pages': 0,
            'timeout_errors': 0,
            'rate_limit_errors': 0
        }
        
        while True:
            try:
                # Формируем URL
                api_url = f"https://catalog.wb.ru/sellers/catalog?appType=1&curr=rub&dest=-1257786&page={page}&sort=popular&supplier={seller_id}"
                
                print(f"\n--- Страница {page} ---")
                print(f"Текущее количество товаров: {len(products)}")
                
                # Большая пауза каждые 1000 товаров
                if len(products) > 0 and len(products) % 1000 == 0:
                    pause_time = 15  # Увеличиваем паузу до 15 секунд
                    print(f"⏸️  БОЛЬШАЯ ПАУЗА {pause_time} сек после {len(products)} товаров...")
                    time.sleep(pause_time)
                
                # Средняя пауза каждые 20 страниц
                if page > 1 and page % 20 == 0:
                    pause_time = 8  # Увеличиваем паузу до 8 секунд
                    print(f"⏸️  Пауза {pause_time} сек после {page} страниц...")
                    time.sleep(pause_time)
                
                # Делаем запрос с увеличенным таймаутом
                print(f"→ Отправка запроса...")
                start_time = time.time()
                
                response = requests.get(
                    api_url, 
                    headers=self.headers, 
                    timeout=30  # Увеличенный таймаут
                )
                
                request_time = time.time() - start_time
                print(f"← Ответ получен за {request_time:.2f} сек, статус: {response.status_code}")
                
                total_requests += 1
                stats['total_requests'] = total_requests
                
                # Проверка статуса
                if response.status_code == 429:
                    stats['rate_limit_errors'] += 1
                    wait_time = 60
                    print(f"⚠️  Ошибка 429: Слишком много запросов. Ожидание {wait_time} сек...")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code != 200:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    print(f"✗ Ошибка ответа: {error_msg}")
                    stats['errors'].append({'page': page, 'error': error_msg})
                    consecutive_errors += 1
                    
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"✗ Слишком много ошибок подряд ({consecutive_errors}). Остановка.")
                        break
                    
                    time.sleep(5)
                    continue
                
                # Парсинг JSON
                try:
                    data = response.json()
                except json.JSONDecodeError as e:
                    error_msg = f"JSON decode error: {e}"
                    print(f"✗ Ошибка парсинга JSON: {error_msg}")
                    print(f"   Ответ сервера: {response.text[:500]}...")
                    stats['errors'].append({'page': page, 'error': error_msg})
                    consecutive_errors += 1
                    
                    if consecutive_errors >= max_consecutive_errors:
                        break
                    continue
                
                # Проверка структуры данных
                if not isinstance(data, dict):
                    print(f"✗ Неверный формат данных: {type(data)}")
                    break
                
                if 'data' not in data:
                    print(f"✗ Отсутствует поле 'data' в ответе")
                    print(f"   Структура: {list(data.keys()) if isinstance(data, dict) else 'не словарь'}")
                    break
                
                if not isinstance(data.get('data'), dict):
                    print(f"✗ Поле 'data' не является словарем: {type(data.get('data'))}")
                    break
                
                if 'products' not in data['data']:
                    print(f"✗ Отсутствует поле 'products' в data")
                    stats['empty_pages'] += 1
                    break
                
                products_on_page = data['data']['products']
                
                if not isinstance(products_on_page, list):
                    print(f"✗ products не является списком: {type(products_on_page)}")
                    break
                
                if not products_on_page:
                    print(f"✓ Страница {page} пустая - достигнут конец каталога")
                    stats['empty_pages'] += 1
                    break
                
                print(f"✓ Найдено товаров на странице: {len(products_on_page)}")
                consecutive_errors = 0  # Сброс счетчика ошибок
                
                # Обработка товаров
                page_errors = 0
                for i, product in enumerate(products_on_page):
                    try:
                        if not isinstance(product, dict):
                            page_errors += 1
                            continue
                            
                        product_info = self.extract_product_info(product)
                        products.append(product_info)
                        
                        # Микропауза каждые 100 товаров
                        if len(products) % 100 == 0:
                            time.sleep(0.5)  # Увеличиваем микропаузу до 0.5 секунд
                        
                        # Прогресс каждые 500 товаров
                        if len(products) % 500 == 0:
                            print(f"   ✓ Обработано товаров: {len(products)}")
                            
                    except Exception as e:
                        page_errors += 1
                        if page_errors <= 3:  # Логируем только первые 3 ошибки
                            print(f"   ⚠️  Ошибка товара {i}: {e}")
                
                if page_errors > 0:
                    print(f"   ⚠️  Ошибок на странице: {page_errors}")
                
                # Проверка на последнюю страницу
                if len(products_on_page) < 100:
                    print(f"✓ Последняя страница (товаров < 100)")
                    break
                
                # Ограничение для безопасности
                if len(products) >= 100000:  # Увеличиваем лимит до 100,000
                    print(f"✓ Достигнут лимит в 100,000 товаров")
                    break
                
                # Следующая страница
                page += 1
                stats['total_pages'] = page
                
                # Базовая пауза между страницами
                time.sleep(3)  # Увеличиваем базовую паузу до 3 секунд
                
            except requests.exceptions.Timeout:
                stats['timeout_errors'] += 1
                consecutive_errors += 1
                print(f"⚠️  Таймаут на странице {page}. Попытка {consecutive_errors}/{max_consecutive_errors}")
                
                if consecutive_errors >= max_consecutive_errors:
                    print(f"✗ Слишком много таймаутов. Остановка.")
                    break
                    
                time.sleep(10)  # Увеличиваем паузу при таймауте до 10 секунд
                continue
                
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: {str(e)}"
                print(f"⚠️  Ошибка соединения: {error_msg}")
                stats['errors'].append({'page': page, 'error': error_msg})
                consecutive_errors += 1
                
                if consecutive_errors >= max_consecutive_errors:
                    break
                    
                time.sleep(15)  # Увеличиваем паузу при ошибке соединения до 15 секунд
                continue
                
            except KeyboardInterrupt:
                print(f"\n⚠️  Прерывание пользователем")
                break
                
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"✗ НЕОЖИДАННАЯ ОШИБКА на странице {page}: {error_msg}")
                import traceback
                traceback.print_exc()
                stats['errors'].append({'page': page, 'error': error_msg})
                consecutive_errors += 1
                
                if consecutive_errors >= max_consecutive_errors:
                    break
                    
                time.sleep(10)  # Увеличиваем паузу при неожиданной ошибке до 10 секунд
        
        # Итоговая статистика
        print(f"\n{'='*50}")
        print(f"ИТОГИ ПАРСИНГА:")
        print(f"  • Всего товаров: {len(products)}")
        print(f"  • Обработано страниц: {stats['total_pages']}")
        print(f"  • Всего запросов: {stats['total_requests']}")
        print(f"  • Ошибок таймаута: {stats['timeout_errors']}")
        print(f"  • Ошибок rate limit: {stats['rate_limit_errors']}")
        print(f"  • Пустых страниц: {stats['empty_pages']}")
        print(f"  • Всего ошибок: {len(stats['errors'])}")
        
        if stats['errors']:
            print(f"\nПоследние ошибки:")
            for err in stats['errors'][-5:]:
                print(f"  - Страница {err['page']}: {err['error'][:100]}...")
        
        print(f"{'='*50}\n")
        
        return products
    
    def get_product_stocks(self, product_id):
        """Получение точных остатков товара через отдельный API"""
        try:
            # API для получения остатков
            stocks_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
            
            response = requests.get(stocks_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            total_stock = 0
            if data.get('data') and data['data'].get('products'):
                product = data['data']['products'][0]
                if 'sizes' in product:
                    for size in product['sizes']:
                        if 'stocks' in size:
                            for stock in size['stocks']:
                                qty = stock.get('qty', 0)
                                if qty and qty > 0:
                                    total_stock += qty
            
            return total_stock
        except:
            return 0
    
    def extract_product_info(self, product_data):
        """Извлечение информации о товаре"""
        product_id = str(product_data.get('id', ''))
        name = product_data.get('name', '')
        brand = product_data.get('brand', '')
        
        price = product_data.get('priceU', 0) / 100 if product_data.get('priceU') else 0
        sale_price = product_data.get('salePriceU', 0) / 100 if product_data.get('salePriceU') else 0
        
        rating = product_data.get('rating', 0)
        feedbacks = product_data.get('feedbacks', 0)
        
        product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
        
        colors = product_data.get('colors', [])
        sizes = product_data.get('sizes', [])
        
        # Определяем статус товара и остатки
        total_stock = 0
        is_available = False
        
        # Метод 1: Проверка sizes и stocks
        if sizes:
            for size in sizes:
                if isinstance(size, dict):
                    # Проверяем наличие товара
                    if size.get('available', False):
                        is_available = True
                    
                    # Считаем остатки
                    if 'stocks' in size and isinstance(size['stocks'], list):
                        for stock in size['stocks']:
                            if isinstance(stock, dict):
                                qty = stock.get('qty', 0)
                                if isinstance(qty, (int, float)) and qty > 0:
                                    total_stock += qty
        
        # Метод 2: Проверка общих полей
        if total_stock == 0:
            for field in ['qty', 'quantity', 'volume', 'totalQuantity']:
                if field in product_data:
                    value = product_data.get(field, 0)
                    if isinstance(value, (int, float)) and value > 0:
                        total_stock = value
                        is_available = True
                        break
        
        # Определяем статус товара
        status = "В продаже" if is_available else "Нет в продаже"
        
        # Извлекаем описание товара
        description = product_data.get('description', '')
        
        return {
            'Наименование': name,
            'Ссылка': product_url,
            'Артикул': product_id,
            'Бренд': brand,
            'Оценка': rating,
            'Количество отзывов': feedbacks,
            'Цена со скидкой': sale_price,
            'Цена без скидки': price,
            'Цвета': ', '.join([c.get('name', '') for c in colors]) if colors else '',
            'Размеры': ', '.join([str(s.get('origName', '')) for s in sizes]) if sizes else '',
            'Категория': product_data.get('subjectName', ''),
            'Остаток': total_stock if total_stock > 0 else 0,
            'Статус': status,
            'Описание': description
        }
    
    def search_product_position(self, product_url, keyword):
        """Поиск позиции товара по ключевому слову"""
        print(f"\n=== НАЧАЛО ПОИСКА ===")
        print(f"URL товара: {product_url}")
        print(f"Ключевое слово: {keyword}")
        
        try:
            # Извлекаем ID товара из URL
            match = re.search(r'/catalog/(\d+)/', product_url)
            if not match:
                print(f"ОШИБКА: Не удалось извлечь ID из URL: {product_url}")
                return 0
            
            product_id = match.group(1)
            print(f"ID товара: {product_id}")
            
            position = 0
            page = 1
            
            while page <= 10:  # Ищем в первых 10 страницах
                try:
                    # Формируем URL для поиска
                    search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&page={page}&query={requests.utils.quote(keyword)}&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false"
                    
                    print(f"\nСтраница {page}: {search_url}")
                    
                    # Делаем запрос
                    response = requests.get(search_url, headers=self.headers, timeout=10)
                    print(f"Статус ответа: {response.status_code}")
                    response.raise_for_status()
                    
                    # Парсим JSON
                    try:
                        data = response.json()
                    except json.JSONDecodeError as e:
                        print(f"ОШИБКА декодирования JSON: {e}")
                        print(f"Ответ сервера: {response.text[:500]}")
                        break
                    
                    # Проверяем структуру данных
                    if not isinstance(data, dict):
                        print(f"ОШИБКА: Неожиданный тип данных: {type(data)}")
                        break
                    
                    if 'data' not in data:
                        print(f"ОШИБКА: Нет поля 'data' в ответе")
                        print(f"Структура ответа: {list(data.keys())}")
                        break
                    
                    if not isinstance(data['data'], dict):
                        print(f"ОШИБКА: data не является словарем: {type(data['data'])}")
                        break
                    
                    if 'products' not in data['data']:
                        print(f"ОШИБКА: Нет поля 'products' в data")
                        print(f"Структура data: {list(data['data'].keys())}")
                        break
                    
                    products = data['data']['products']
                    if not isinstance(products, list):
                        print(f"ОШИБКА: products не является списком: {type(products)}")
                        break
                    
                    print(f"Найдено товаров: {len(products)}")
                    
                    # Проверяем каждый товар
                    for i, product in enumerate(products):
                        position += 1
                        
                        if not isinstance(product, dict):
                            print(f"Товар {i} не является словарем")
                            continue
                        
                        # Получаем ID текущего товара
                        current_id = product.get('id')
                        
                        # Выводим первые несколько товаров для отладки
                        if i < 3:
                            print(f"Товар {position}: ID={current_id}, тип={type(current_id)}")
                        
                        # Безопасное сравнение
                        if current_id is not None:
                            try:
                                # Преобразуем оба значения в строки
                                current_id_str = str(current_id).strip()
                                product_id_str = str(product_id).strip()
                                
                                if current_id_str == product_id_str:
                                    print(f"\n✓ ТОВАР НАЙДЕН на позиции {position}!")
                                    return position
                            except Exception as e:
                                print(f"ОШИБКА при сравнении ID: {e}")
                                print(f"current_id={current_id}, product_id={product_id}")
                    
                    # Проверяем, есть ли еще страницы
                    if len(products) < 100:
                        print(f"Последняя страница (товаров: {len(products)})")
                        break
                    
                    page += 1
                    time.sleep(0.5)
                    
                except requests.exceptions.RequestException as e:
                    print(f"ОШИБКА сети: {e}")
                    break
                except Exception as e:
                    print(f"НЕОЖИДАННАЯ ОШИБКА: {e}")
                    import traceback
                    traceback.print_exc()
                    break
            
            print(f"\n✗ Товар НЕ НАЙДЕН после проверки {position} позиций")
            return 0
            
        except Exception as e:
            print(f"\nКРИТИЧЕСКАЯ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()
            return 0
        finally:
            print("=== КОНЕЦ ПОИСКА ===\n")

    def analyze_ad_rates(self, query, region):
        results = []
        page = 1
        position = 0
        try:
            search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest={region}&page={page}&query={requests.utils.quote(query)}&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false"
            response = requests.get(search_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get('data') and data['data'].get('products'):
                for product in data['data']['products']:
                    position += 1
                    article = product.get('id')
                    name = product.get('name', '')
                    cpm = int(product.get('cpm', 0) or 0)
                    bid = int(product.get('bid', 0) or 0)
                    results.append({
                        'position': position,
                        'type': product.get('advertType', 'Поиск'),
                        'article': article,
                        'name': name,
                        'cpm': cpm,
                        'bid': bid
                    })
            return results
        except Exception as e:
            print(f"Ошибка при анализе ставок: {str(e)}")
            import traceback
            traceback.print_exc()
            return results

    def analyze_competitors(self, product_url):
        try:
            match = re.search(r'/catalog/(\d+)/', product_url)
            if not match:
                return []
            product_id = match.group(1)
            product_info_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
            response = requests.get(product_info_url, headers=self.headers)
            if response.status_code != 200:
                return []
            data = response.json()
            if not data.get('data') or not data['data'].get('products'):
                return []
            product_data = data['data']['products'][0]
            category = product_data.get('subjectName', '')
            # fallback: если нет категории, пробуем парсить из HTML
            if not category:
                category = self.get_category_from_html(product_url)
            competitors = self.analyze_competitors_seo(category)
            return competitors
        except Exception as e:
            print(f"Ошибка при анализе конкурентов: {str(e)}")
            return []

    def analyze_seo(self, product_url):
        print(f"\n=== АНАЛИЗ SEO ===")
        print(f"URL товара: {product_url}")
        try:
            product_url = product_url.strip()
            if product_url.startswith('@'):
                product_url = product_url[1:].strip()
            print(f"URL товара (после удаления @): {product_url}")
            if not product_url:
                print("ОШИБКА: URL товара пустой")
                return {'error': 'Ссылка на товар не указана'}
            match = re.search(r'/catalog/(\d+)/', product_url)
            if not match:
                print(f"ОШИБКА: Не удалось найти ID товара в URL: {product_url}")
                return {'error': 'Неверный формат ссылки на товар Wildberries'}
            product_id = match.group(1)
            product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
            print(f"URL преобразован в: {product_url}")
            product_info_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
            print(f"Запрашиваем информацию по URL: {product_info_url}")
            response = requests.get(product_info_url, headers=self.headers)
            print(f"Статус ответа: {response.status_code}")
            if response.status_code != 200:
                print(f"ПРОБЛЕМА 2: Ошибка при получении данных товара. Статус: {response.status_code}")
                raise ValueError(f"Ошибка при получении данных товара: {response.status_code}")
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                print(f"ПРОБЛЕМА 3: Ошибка при разборе JSON ответа: {e}")
                raise ValueError("Ошибка при разборе данных товара")
            if not data.get('data') or not data['data'].get('products'):
                print("ПРОБЛЕМА 5: В ответе отсутствуют данные о товаре")
                raise ValueError("Товар не найден")
            product_data = data['data']['products'][0]
            current_description = product_data.get('description', '')
            category = product_data.get('subjectName', '')
            # Если описания нет в API — fallback на requests/BeautifulSoup
            if not current_description or not current_description.strip():
                print("ПРОБЛЕМА 6: У товара отсутствует описание в API, пробуем HTML")
                current_description = self.get_description_from_html(product_url)
            if not current_description or not current_description.strip():
                # Только если не найдено — Playwright
                print("ПРОБЛЕМА 7: Описание не найдено в HTML, пробуем Playwright")
                current_description = self.get_description_playwright(product_url)
            if not current_description or not current_description.strip():
                current_description = "Описание отсутствует"
            print(f"Категория: {category}")
            print(f"Длина описания: {len(current_description)} символов")
            try:
                keywords = self.extract_keywords(current_description)
                print(f"Найдено ключевых слов: {len(keywords)}")
            except Exception as e:
                print(f"ПРОБЛЕМА 8: Ошибка при извлечении ключевых слов: {e}")
                keywords = []
            try:
                competitors = self.analyze_competitors_seo(category)
                print(f"Проанализировано конкурентов: {len(competitors)}")
            except Exception as e:
                print(f"Ошибка при анализе конкурентов: {e}")
                competitors = []
            try:
                recommendations = self.generate_seo_recommendations(
                    current_description,
                    keywords,
                    competitors
                )
            except Exception as e:
                print(f"Ошибка при генерации рекомендаций: {e}")
                recommendations = []
            try:
                optimized_description = self.generate_optimized_description(
                    current_description,
                    keywords,
                    competitors,
                    recommendations
                )
            except Exception as e:
                print(f"Ошибка при генерации оптимизированного описания: {e}")
                optimized_description = current_description
            return {
                'current_description': current_description,
                'keywords': keywords,
                'competitors': competitors,
                'recommendations': recommendations,
                'optimized_description': optimized_description
            }
        except Exception as e:
            print(f"КРИТИЧЕСКАЯ ОШИБКА при анализе SEO: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}

    def get_category_from_html(self, product_url):
        try:
            headers = self.headers.copy()
            headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
            response = requests.get(product_url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            cat = soup.find('span', {'class': 'breadcrumbs__item'}).text.strip()
            return cat
        except Exception as e:
            print(f"Ошибка при парсинге категории из HTML: {e}")
            return ''

def save_to_csv(products, filename):
    """Сохранение продуктов в CSV файл"""
    if not products:
        return None
    
    headers = ['№'] + list(products[0].keys())
    
    with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        
        for i, product in enumerate(products, 1):
            row = [i] + list(product.values())
            writer.writerow(row)
    
    return filename

def save_to_xlsx(products, filename):
    """Сохранение продуктов в XLSX файл"""
    if not products:
        return None
    try:
        # Создаем DataFrame
        df = pd.DataFrame(products)
        # Добавляем номер строки
        df.insert(0, '№', range(1, len(df) + 1))
        # Сохраняем в Excel
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Товары')
            # Получаем рабочий лист
            worksheet = writer.sheets['Товары']
            # Настраиваем ширину столбцов
            for idx, col in enumerate(df.columns):
                max_length = max(
                    df[col].astype(str).apply(len).max(),
                    len(str(col))
                )
                worksheet.column_dimensions[chr(65 + idx)].width = min(max_length + 2, 50)
        print(f"✓ XLSX файл успешно создан: {filename}")
        return filename
    except Exception as e:
        print(f"✗ Ошибка при создании XLSX файла: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def save_to_pdf(products, filename):
    """Сохранение продуктов в PDF файл"""
    if not products:
        return None
    try:
        # Создаем PDF документ
        doc = SimpleDocTemplate(
            filename,
            pagesize=landscape(letter),
            rightMargin=30,
            leftMargin=30,
            topMargin=30,
            bottomMargin=30
        )
        # Подготавливаем данные для таблицы
        headers = ['№'] + list(products[0].keys())
        data = [headers]
        for i, product in enumerate(products, 1):
            row = [i] + list(product.values())
            data.append(row)
        # Создаем таблицу
        table = Table(data)
        # Стили для таблицы
        style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ])
        table.setStyle(style)
        # Добавляем таблицу в документ
        elements = []
        elements.append(table)
        # Строим PDF
        doc.build(elements)
        print(f"✓ PDF файл успешно создан: {filename}")
        return filename
    except Exception as e:
        print(f"✗ Ошибка при создании PDF файла: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/')
def index():
    """Главная страница"""
    return send_from_directory('.', 'index.html')

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/status')
def api_status():
    return jsonify({
        "status": "ok",
        "message": "Wildberries Parser API is running",
        "version": "1.0.0"
    })

@app.route('/parse', methods=['POST'])
def parse():
    try:
        data = request.get_json()
        seller_url = data.get('seller_url')
        file_format = data.get('format', 'csv')  # По умолчанию CSV
        print(f"Получен запрос на парсинг. URL: {seller_url}, Формат: {file_format}")  # Отладочный вывод
        if not seller_url:
            return jsonify({'error': 'URL продавца не указан'}), 400
        # Парсинг товаров
        parser = WildberriesParser()
        products = parser.parse_seller_products(seller_url)
        if not products:
            return jsonify({'error': 'Товары не найдены'}), 404
        # Генерация имени файла с учетом формата
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'products_{timestamp}.{file_format}'
        # Сохранение в выбранном формате
        if file_format == 'xlsx':
            save_to_xlsx(products, filename)
        elif file_format == 'pdf':
            save_to_pdf(products, filename)
        else:  # По умолчанию CSV
            save_to_csv(products, filename)
        # Получаем размер файла
        file_size = os.path.getsize(filename)
        file_size_mb = round(file_size / (1024 * 1024), 2)
        print(f"Файл сохранен: {filename}, размер: {file_size_mb} МБ")  # Отладочный вывод
        return jsonify({
            'success': True,
            'products_count': len(products),
            'filename': filename,
            'format': file_format,
            'file_size_mb': file_size_mb
        })
    except Exception as e:
        print(f"Ошибка при парсинге: {str(e)}")  # Отладочный вывод
        return jsonify({'error': f'Ошибка парсинга: {str(e)}'}), 500

@app.route('/check-position', methods=['POST'])
def check_position():
    """Endpoint для проверки позиций товара"""
    print("\n=== НОВЫЙ ЗАПРОС check-position ===")
    try:
        data = request.json
        product_url = data.get('product_url')
        keywords = data.get('keywords', [])
        
        print(f"Получены данные:")
        print(f"  URL: {product_url}")
        print(f"  Ключевые слова: {keywords}")
        
        if not product_url or not keywords:
            print("ОШИБКА: Не все параметры указаны")
            return jsonify({'error': 'Не все параметры указаны'}), 400
        
        parser = WildberriesParser()
        results = []
        
        for keyword in keywords[:10]:
            if keyword and keyword.strip():
                try:
                    print(f"\nПоиск для ключевого слова: '{keyword}'")
                    position = parser.search_product_position(product_url, keyword.strip())
                    
                    # Гарантируем, что position - это целое число >= 0
                    if position is None or not isinstance(position, (int, float)) or position < 0:
                        position = 0
                    else:
                        position = int(position)
                    
                    results.append({
                        'keyword': keyword.strip(),
                        'position': position
                    })
                    print(f"Результат: позиция {position}")
                    
                except Exception as e:
                    print(f"ОШИБКА при поиске для '{keyword}': {e}")
                    import traceback
                    traceback.print_exc()
                    results.append({
                        'keyword': keyword.strip(),
                        'position': 0
                    })
                
                time.sleep(0.5)
        
        print(f"\nИтоговые результаты: {results}")
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        print(f"\nКРИТИЧЕСКАЯ ОШИБКА в check_position: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-adrates', methods=['POST'])
def analyze_adrates():
    """Endpoint для анализа рекламных ставок"""
    try:
        data = request.json
        query = data.get('query')
        region = data.get('region', '-1114822')
        
        if not query:
            return jsonify({'error': 'Поисковый запрос не указан'}), 400
        
        parser = WildberriesParser()
        results = parser.analyze_ad_rates(query, region)
        
        if not results:
            return jsonify({'error': 'Не удалось получить данные о ставках. Попробуйте другой запрос.'}), 404
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-competitors', methods=['POST'])
def analyze_competitors():
    """Endpoint для анализа конкурентов"""
    try:
        data = request.json
        product_url = data.get('product_url')
        
        if not product_url:
            return jsonify({'error': 'Ссылка на товар не указана'}), 400
        
        parser = WildberriesParser()
        results = parser.analyze_competitors(product_url)
        
        return jsonify(results)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-seo', methods=['POST'])
def analyze_seo():
    """Endpoint для анализа SEO"""
    print("\n=== НОВЫЙ ЗАПРОС analyze-seo ===")
    try:
        data = request.get_json()
        product_url = data.get('product_url')
        if not product_url:
            return jsonify({'error': 'URL не найден в данных'}), 400
        parser = WildberriesParser()
        results = parser.analyze_seo(product_url)
        return jsonify(results)
    except Exception as e:
        print(f"ОШИБКА при анализе SEO: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    try:
        print(f"Запрос на скачивание файла: {filename}")  # Отладочный вывод
        return send_file(filename, as_attachment=True)
    except Exception as e:
        print(f"Ошибка при скачивании файла: {str(e)}")  # Отладочный вывод
        return jsonify({'error': 'Файл не найден'}), 404

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)