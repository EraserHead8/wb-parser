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

app = Flask(__name__)
CORS(app)
#тестовая строка Макса
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
        try:
            seller_id = self.get_seller_id(seller_url)
            print(f"ID продавца: {seller_id}")
        except Exception as e:
            print(f"Ошибка извлечения ID: {e}")
            raise
            
        products = []
        page = 1
        
        while True:
            # Используем правильный API endpoint
            api_url = f"https://catalog.wb.ru/sellers/catalog?appType=1&curr=rub&dest=-1257786&page={page}&sort=popular&supplier={seller_id}"
            
            print(f"Запрос к API: {api_url}")
            
            try:
                response = requests.get(api_url, headers=self.headers)
                response.raise_for_status()
                
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    print(f"Ошибка декодирования JSON: {response.text[:200]}")
                    break
                
                if not data.get('data') or not data['data'].get('products'):
                    print(f"Нет товаров на странице {page}")
                    break
                
                products_on_page = data['data']['products']
                print(f"Найдено товаров на странице {page}: {len(products_on_page)}")
                
                for product in products_on_page:
                    product_info = self.extract_product_info(product)
                    products.append(product_info)
                
                # Если товаров меньше 100, значит это последняя страница
                if len(products_on_page) < 100:
                    break
                    
                page += 1
                time.sleep(1)  # Увеличиваем задержку
                
            except requests.exceptions.RequestException as e:
                print(f"Ошибка запроса на странице {page}: {str(e)}")
                break
            except Exception as e:
                print(f"Общая ошибка на странице {page}: {str(e)}")
                break
        
        print(f"Всего найдено товаров: {len(products)}")
        return products
    
    def extract_product_info(self, product_data):
        """Извлечение информации о товаре"""
        product_id = product_data.get('id', '')
        name = product_data.get('name', '')
        brand = product_data.get('brand', '')
        
        price = product_data.get('priceU', 0) / 100
        sale_price = product_data.get('salePriceU', 0) / 100
        
        rating = product_data.get('rating', 0)
        feedbacks = product_data.get('feedbacks', 0)
        
        product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
        
        colors = product_data.get('colors', [])
        sizes = product_data.get('sizes', [])
        
        return {
            'Наименование': name,
            'Ссылка': product_url,
            'Артикул': str(product_id),
            'Бренд': brand,
            'Оценка': rating,
            'Количество отзывов': feedbacks,
            'Цена со скидкой': sale_price,
            'Цена без скидки': price,
            'Цвета': ', '.join([c.get('name', '') for c in colors]) if colors else '',
            'Размеры': ', '.join([str(s.get('origName', '')) for s in sizes]) if sizes else '',
            'Категория': product_data.get('subjectName', ''),
            'Остаток': product_data.get('volume', 0)
        }
    
    def search_product_position(self, product_url, keyword):
        """Поиск позиции товара по ключевому слову"""
        match = re.search(r'/catalog/(\d+)/', product_url)
        if not match:
            return -1
        
        product_id = match.group(1)
        print(f"Ищем товар ID: {product_id} по запросу: {keyword}")
        
        position = 0
        page = 1
        
        while page <= 10:  # Ищем в первых 10 страницах (1000 товаров)
            # Используем правильный API для поиска
            search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&page={page}&query={requests.utils.quote(keyword)}&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false"
            
            try:
                response = requests.get(search_url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                
                if data.get('data') and data['data'].get('products'):
                    products = data['data']['products']
                    print(f"Страница {page}: найдено {len(products)} товаров")
                    
                    # Проверяем каждый товар на странице
                    for i, product in enumerate(products):
                        position += 1
                        if str(product.get('id')) == product_id:
                            print(f"Товар найден на позиции {position}!")
                            return position
                    
                    # Если товаров меньше 100, значит это последняя страница
                    if len(products) < 100:
                        break
                else:
                    print(f"На странице {page} товаров не найдено")
                    break
                
                page += 1
                time.sleep(0.5)  # Небольшая задержка между запросами
                
            except Exception as e:
                print(f"Ошибка при поиске на странице {page}: {str(e)}")
                break
        
        print(f"Товар не найден в топ-{position}")
        return -1  # Товар не найден

def save_to_csv(products):
    """Сохранение продуктов в CSV файл"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'wildberries_products_{timestamp}.csv'
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    
    if not products:
        return None
    
    # Получаем заголовки из первого продукта
    headers = ['№'] + list(products[0].keys())
    
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        
        for i, product in enumerate(products, 1):
            row = [i] + list(product.values())
            writer.writerow(row)
    
    return filename

@app.route('/')
def index():
    """Главная страница"""
    return send_from_directory('.', 'index.html')

@app.route('/parse', methods=['POST'])
def parse_products():
    """Endpoint для парсинга товаров продавца"""
    try:
        data = request.json
        seller_url = data.get('seller_url')
        
        if not seller_url:
            return jsonify({'error': 'URL продавца не указан'}), 400
        
        parser = WildberriesParser()
        products = parser.parse_seller_products(seller_url)
        
        if not products:
            return jsonify({'error': 'Товары не найдены'}), 404
        
        # Сохраняем в CSV (Excel может открыть CSV файлы)
        filename = save_to_csv(products)
        
        if not filename:
            return jsonify({'error': 'Ошибка при сохранении файла'}), 500
        
        return jsonify({
            'success': True,
            'filename': filename,
            'products_count': len(products)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/check-position', methods=['POST'])
def check_position():
    """Endpoint для проверки позиций товара"""
    try:
        data = request.json
        product_url = data.get('product_url')
        keywords = data.get('keywords', [])
        
        if not product_url or not keywords:
            return jsonify({'error': 'Не все параметры указаны'}), 400
        
        parser = WildberriesParser()
        results = []
        
        for keyword in keywords[:10]:
            position = parser.search_product_position(product_url, keyword)
            results.append({
                'keyword': keyword,
                'position': position if position > 0 else 0
            })
            time.sleep(0.5)
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """Endpoint для скачивания файла"""
    try:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            # Изменяем расширение на .xlsx для лучшей совместимости
            download_name = filename.replace('.csv', '.csv')
            return send_file(
                filepath, 
                as_attachment=True,
                download_name=download_name,
                mimetype='text/csv'
            )
        else:
            return jsonify({'error': 'Файл не найден'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)